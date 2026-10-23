# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The malfunction-report API.

`POST /api/method/brchms.brchms_support.api.create_malfunction_report`

A user in the Rest app presses "Report a malfunction", the app forwards what they
wrote here, and this method records it as a `Rest Malfunction Report` — a row the
support team reads and works in the Desk.

## Why a whitelisted method instead of a document write

The app's credential holds the read-only `Rest Integration` role and must not be
given create permission on this doctype: a token that can write any record is a
token that can forge one. The whitelisted method IS the authorisation boundary
(same design as `proposals.create_proposal`), and it inserts with
`ignore_permissions` on purpose. The boundary is enforced here:

  * every supplied string is trimmed and length-capped;
  * the category is one of the app's own values;
  * a screenshot must be a real PNG/JPEG/WebP under 5 MB, decoded here;
  * one deployment may file 200 reports an hour (a stuck retry loop must not
    flood the Desk);
  * a replay of `(instance_id, client_request_id)` returns the row already
    recorded instead of creating a second one — the UI reuses its request id
    when a submit fails, so a lost response can be retried safely.

## Why the unique index is installed by `after_migrate`

Same reason as `purchase_reference` in `brchms_credits`: Frappe creates a unique
index only for a field used as the document NAME, so the doctype's `unique: 1`
flag alone would look right and constrain nothing. `after_migrate()` installs the
composite index idempotently; hooks.py points at it.
"""

import base64
import binascii
import os
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime

DOCTYPE = "Rest Malfunction Report"

#: The categories the Rest app sends. Kept in step with the doctype's Select and
#: with `MALFUNCTION_REPORT_CATEGORIES` in the app's backend.
CATEGORIES = (
	"app_error",
	"data_error",
	"access_login",
	"performance",
	"other",
)

#: Screenshot content types the app may send, and the extension each is stored
#: under. The extension is chosen from the MIME type, never from the filename the
#: client supplied.
SCREENSHOT_EXTENSIONS = {
	"image/png": ".png",
	"image/jpeg": ".jpg",
	"image/webp": ".webp",
}

MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
MAX_DESCRIPTION_CHARS = 4000
MAX_PAGE_URL_CHARS = 600
MAX_APP_VERSION_CHARS = 50
MAX_USER_AGENT_CHARS = 500
MAX_CLIENT_REQUEST_ID_CHARS = 100
MAX_INSTANCE_ID_CHARS = 60
MAX_REPORTER_ID_CHARS = 100
MAX_ORGANIZATION_ID_CHARS = 100
MAX_NAME_CHARS = 200
MAX_EMAIL_CHARS = 200
MAX_SCREENSHOT_FILENAME_CHARS = 140

#: Per-deployment budget. A report is a human action, so 200 an hour is far above
#: any legitimate use and far below what a retry loop would generate.
RATE_LIMIT_PER_HOUR = 200
RATE_WINDOW_SECONDS = 3600
RATE_LIMIT_CACHE_PREFIX = "brchms_support:create_malfunction_report"

#: 8-100 characters: the app sends a UUID, but the check only requires something
#: stable and safe to store — not a particular generator.
CLIENT_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,99}$")

#: Frappe's default `Text`/`Data` columns are generous, but a report is not a
#: place to store a log file. These match the app's own validation caps.
INDEX_EXISTS_SQL = "SHOW INDEX FROM `{0}` WHERE Key_name = %s"
UNIQUE_INDEX_NAME = "unique_instance_client_request"


@frappe.whitelist()
def create_malfunction_report(
	instance_id=None,
	client_request_id=None,
	organization_id=None,
	organization_name=None,
	reporter_user_id=None,
	reporter_name=None,
	reporter_email=None,
	category=None,
	description=None,
	page_url=None,
	app_version=None,
	user_agent=None,
	screenshot_base64=None,
	screenshot_mime=None,
	screenshot_filename=None,
):
	"""
	Record one malfunction report and return `{name, status, reported_at}`.

	The app calls this with the reporter's words and, optionally, one screenshot.
	Every refusal names the field it is about (the app binds those to the form),
	and nothing is written before the refusal.
	"""
	instance_id = _required_text(instance_id, "instance_id", _("The deployment identity is missing."), MAX_INSTANCE_ID_CHARS)
	client_request_id = _validated_request_id(client_request_id)
	category = _validated_category(category)
	description = _validated_description(description)

	organization_id = _optional_text(organization_id, MAX_ORGANIZATION_ID_CHARS)
	organization_name = _optional_text(organization_name, MAX_NAME_CHARS)
	reporter_user_id = _required_text(
		reporter_user_id, "reporter_user_id", _("The report does not say who filed it."), MAX_REPORTER_ID_CHARS
	)
	reporter_name = _optional_text(reporter_name, MAX_NAME_CHARS)
	reporter_email = _validated_email(reporter_email)
	page_url = _optional_text(page_url, MAX_PAGE_URL_CHARS)
	app_version = _optional_text(app_version, MAX_APP_VERSION_CHARS)
	user_agent = _optional_text(user_agent, MAX_USER_AGENT_CHARS)
	screenshot = _validated_screenshot(screenshot_base64, screenshot_mime, screenshot_filename)

	# A replay is an answer, not an attempt: it returns the row already recorded
	# BEFORE the rate limit counts it. The UI retries with the same request id when
	# a response is lost, and that retry must not consume the hourly budget.
	existing = _existing_report(instance_id, client_request_id)
	if existing:
		return _as_response(existing)

	_enforce_rate_limit(instance_id)

	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"client_request_id": client_request_id,
			"instance_id": instance_id,
			"organization_id": organization_id or None,
			"organization_name": organization_name or None,
			"reporter_user_id": reporter_user_id,
			"reporter_name": reporter_name or None,
			"reporter_email": reporter_email or None,
			"category": category,
			"description": description,
			"page_url": page_url or None,
			"app_version": app_version or None,
			"user_agent": user_agent or None,
			"source": "Rest app",
		}
	)

	try:
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# The loser of a concurrent replay: the row exists now, so answer with it
		# rather than reporting a conflict the reporter did not cause.
		frappe.db.rollback()
		existing = _existing_report(instance_id, client_request_id)
		if existing:
			return _as_response(existing)
		raise

	if screenshot:
		_attach_screenshot(doc, *screenshot)

	# Explicit commit, not the request's implicit one: the row and its private
	# file are the deliverable, and a later exception in this request must not
	# discard a report the user believes was filed.
	frappe.db.commit()

	return _as_response({"name": doc.name, "status": doc.status, "reported_at": doc.reported_at})


def after_migrate():
	"""
	Called by `bench migrate` via the hook in `hooks.py`.

	Idempotent and silent when the index already exists — a migration runs on
	every deploy and must not fail because its work was already done.
	"""
	if ensure_uniqueness_constraint():
		frappe.logger().info("brchms: installed the unique malfunction-report client_request_id index")


def ensure_uniqueness_constraint():
	"""
	Install the per-deployment idempotency key as a real unique index.

	Idempotent: absent means the work is done. `frappe.db.add_unique` is used
	rather than raw DDL because Frappe refuses DDL through `db.sql` inside the
	migration's transaction; `add_unique` commits around the statement itself.
	"""
	index_name = UNIQUE_INDEX_NAME
	try:
		existing = frappe.db.sql(
			INDEX_EXISTS_SQL.format("tab{0}".format(DOCTYPE)), (index_name,)
		)
	except Exception:
		return False

	if existing:
		return False

	frappe.db.add_unique(
		DOCTYPE, ["instance_id", "client_request_id"], constraint_name=index_name
	)
	return True


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────


def _throw(message):
	"""Refuse with a message the app can bind to a field: `<field>: <reason>`."""
	frappe.throw(_(message))


def _required_text(value, field, message, max_chars):
	text = cstr(value).strip()
	if not text:
		_throw("{0}: {1}".format(field, message))
	if len(text) > max_chars:
		_throw(
			"{0}: Keep this under {1} characters.".format(field, max_chars)
		)
	return text


def _optional_text(value, max_chars):
	text = cstr(value).strip()
	if not text:
		return ""
	if len(text) > max_chars:
		return text[:max_chars]
	return text


def _validated_request_id(value):
	text = cstr(value).strip()
	if not text:
		_throw(_("client_request_id: The report has no request id, so a retry could duplicate it."))
	if len(text) > MAX_CLIENT_REQUEST_ID_CHARS or not CLIENT_REQUEST_ID_PATTERN.match(text):
		_throw(
			_(
				"client_request_id: Use 8-100 letters, digits, dots, dashes, colons or "
				"underscores, starting with a letter or digit."
			)
		)
	return text


def _validated_category(value):
	text = cstr(value).strip().lower()
	if text not in CATEGORIES:
		_throw(
			_(
				"category: Choose one of app_error, data_error, access_login, "
				"performance or other."
			)
		)
	return text


def _validated_description(value):
	text = cstr(value).strip()
	if not text:
		_throw(_("description: Describe what went wrong."))
	if len(text) > MAX_DESCRIPTION_CHARS:
		_throw(
			_(
				"description: Keep the description under {0} characters; add detail in "
				"the screenshot or a follow-up reply."
			).format(MAX_DESCRIPTION_CHARS)
		)
	return text


def _validated_email(value):
	text = cstr(value).strip()
	if not text:
		return ""
	if len(text) > MAX_EMAIL_CHARS or "@" not in text or "." not in text.rsplit("@", 1)[-1]:
		_throw(_("reporter_email: Enter a valid email address."))
	return text


def _validated_screenshot(base64_value, mime, filename):
	"""
	Decode and vet one screenshot, or return `None`.

	The decoded BYTES are what gets stored: a base64 string is not an image, and
	trusting the claimed MIME type would let a caller put anything in an `Attach
	Image` field. Frappe's own image rendering then sees a real file.
	"""
	mime = cstr(mime).strip().lower()
	encoded = cstr(base64_value).strip()
	filename = cstr(filename).strip()

	if not encoded:
		if mime or filename:
			_throw(
				_(
					"screenshot_base64: Attach the screenshot's data, or leave the "
					"screenshot fields empty."
				)
			)
		return None

	if mime not in SCREENSHOT_EXTENSIONS:
		_throw(_("screenshot_mime: Attach a PNG, JPEG or WebP image."))

	if len(encoded) > (MAX_SCREENSHOT_BYTES * 4 // 3) + 16:
		_throw(_("screenshot_base64: The screenshot is larger than 5 MB. Attach a smaller image."))

	try:
		content = base64.b64decode(encoded, validate=True)
	except (binascii.Error, ValueError):
		_throw(_("screenshot_base64: The screenshot is not valid base64 image data."))

	if not content:
		_throw(_("screenshot_base64: The screenshot is empty."))
	if len(content) > MAX_SCREENSHOT_BYTES:
		_throw(_("screenshot_base64: The screenshot is larger than 5 MB. Attach a smaller image."))

	extension = SCREENSHOT_EXTENSIONS[mime]
	stem = os.path.splitext(os.path.basename(filename or "screenshot"))[0].strip() or "screenshot"
	if len(stem) > 80:
		stem = stem[:80]
	safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-") or "screenshot"

	return (content, mime, "{0}{1}".format(safe_name, extension))


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency, rate limiting and the attachment
# ─────────────────────────────────────────────────────────────────────────────


def _existing_report(instance_id, client_request_id):
	rows = frappe.get_all(
		DOCTYPE,
		filters={"instance_id": instance_id, "client_request_id": client_request_id},
		fields=["name", "status", "reported_at"],
		limit_page_length=1,
	)
	return rows[0] if rows else None


def _as_response(row):
	"""
	The shape the Rest app stores and shows.

	`reported_at` is a Datetime; stringify it because Frappe wraps Python values
	and the app reads a string. `status` is the triage state, not the app's.
	"""
	return {
		"name": cstr(row.get("name")),
		"status": cstr(row.get("status") or "new"),
		"reported_at": cstr(row.get("reported_at") or ""),
	}


def _rate_key(instance_id, moment=None):
	"""The cache key for one deployment's current hour bucket."""
	bucket = (moment or now_datetime()).strftime("%Y%m%d%H")
	return "{0}:{1}:{2}".format(RATE_LIMIT_CACHE_PREFIX, instance_id, bucket)


def _enforce_rate_limit(instance_id):
	"""
	Bound one deployment to 200 reports an hour.

	A best-effort counter: if the cache is unavailable the report is still
	accepted (a report must not fail because a counter could not be read), and the
	window is the calendar hour rather than a sliding one, which is enough to stop
	a stuck retry loop without pretending to be exact rate limiting.
	"""
	key = _rate_key(instance_id)

	try:
		current = cint(frappe.cache().get_value(key) or 0)
	except Exception:
		frappe.logger().warning("brchms: could not read the malfunction-report rate counter")
		return

	if current >= RATE_LIMIT_PER_HOUR:
		_throw(
			_(
				"instance_id: This deployment has filed {0} malfunction reports in the last "
				"hour. Nothing was recorded; wait for the support team to catch up and try "
				"again, or describe the problem there instead."
			).format(RATE_LIMIT_PER_HOUR)
		)

	try:
		frappe.cache().set_value(key, current + 1, expires_in_sec=RATE_WINDOW_SECONDS + 120)
	except Exception:
		frappe.logger().warning("brchms: could not write the malfunction-report rate counter")


def _attach_screenshot(doc, content, _mime, file_name):
	"""
	Store the screenshot as a PRIVATE file attached to the report.

	A malfunction screenshot can show any screen in the app, so it is private by
	default: the Desk's support team sees it, and a future public URL does not.

	`file_manager.save_file` is the supported way to write BYTES on every Frappe
	version this app targets. Constructing a `File` document by hand and putting
	the bytes in `content` depends on that field's decode path, which differs
	between versions.

	A screenshot failure does NOT fail the report: the report is the deliverable
	and the screenshot is supporting evidence, so the error is logged for the
	operator and the report stands without it.
	"""
	from frappe.utils.file_manager import save_file

	try:
		file_doc = save_file(file_name, content, DOCTYPE, doc.name, is_private=1)
		if not file_doc or not file_doc.file_url:
			raise ValueError("no file URL was returned")
		doc.db_set("screenshot", file_doc.file_url, update_modified=False)
	except Exception:
		# The REPORT is the deliverable; the screenshot is supporting evidence.
		# Losing the report because its attachment could not be written would be
		# the wrong trade, so the failure is logged for the operator and the
		# report stands without a screenshot.
		frappe.logger().error(
			"brchms: malfunction report %s was recorded but its screenshot could not be saved",
			doc.name,
			exc_info=True,
		)
