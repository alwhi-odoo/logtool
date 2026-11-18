"""Classification: (method, path, status) -> (type, app, operation) for requests, logger -> family for events.

`classify()` never raises and never returns None.
"""

from __future__ import annotations

import re


# Three independent questions are answered for every request:
#
#   type       what mechanism is it?     ui.read, ui.action, api.write, report, assets, ...
#   app        which Odoo app owns it?   sale, stock, account, website_sale, ...
#   operation  the specific thing        "call_kw sale.order.action_confirm", "/shop/<slug>"
#
# Nothing here is tied to one database or one customer:
#   * the app of an RPC call is the module prefix of its model (sale.order -> sale),
#   * the app of a controller route is the module that owns the URL prefix,
#   * the operation keeps full detail; only ids, tokens and slugs are collapsed so one
#     page does not split into thousands of rows. Nothing is merged into a bigger bucket.

# call_kw / call_button methods, by what the user (or integration) is doing.
READ_METHODS = {
    "read", "search_read", "web_search_read", "web_read", "web_read_group", "read_group",
    "formatted_read_group", "formatted_read_grouping_sets", "search", "search_count",
    "name_search", "web_name_search", "name_get", "display_name", "fields_get",
    "search_panel_select_range", "search_panel_select_multi_range", "get_views", "load_views",
    "read_progress_bar", "web_resequence", "get_formview_id", "get_formview_action",
    "check_access_rights", "has_group", "read_recordset",
}
WRITE_METHODS = {
    "create", "write", "web_save", "unlink", "copy", "copy_data", "web_override_translations",
    "action_archive", "action_unarchive", "toggle_active", "resequence", "onchange_field",
}
ONCHANGE_METHODS = {"onchange", "onchange_form"}


def _split_model_method(s):
    """'account.move.web_read' -> ('account.move', 'web_read')"""
    if not s or "." not in s:
        return s or "?", ""
    model, _, method = s.rpartition(".")
    return model, method


BUTTONISH = re.compile(r"^(action_|button_|toggle_|do_)|_(confirm|validate|post|cancel|done|send|"
                       r"apply|process|reconcile|pay|invoice|print)$")
POSISH = re.compile(r"(^|_)pos(_|$)")                 # Odoo's naming for point-of-sale entry points


def app_of_model(model, method=""):
    """Odoo model names start with the name of the module that owns them."""
    if not model or model == "?":
        return "other"
    if model.startswith("x_"):
        return "studio-custom"                        # Studio-created models
    if POSISH.search(method or "") or model.startswith("pos."):
        return "pos"
    if model.startswith("res.partner"):
        return "contacts"
    head = model.split(".", 1)[0]
    return "base" if head in ("res", "ir") else head


def _rpc_type(model, method, button=False):
    if method in ONCHANGE_METHODS:
        return "onchange"
    if method in WRITE_METHODS:
        return "write"
    if model.startswith("base_import") or "import" in method:
        return "import"
    if "export" in method:
        return "export"
    is_report = model.endswith(".report") or ".report" in model or model.startswith("report.")
    if method in READ_METHODS:
        return "report" if is_report else "read"
    if "report" in method or is_report:
        return "report"
    if button or BUTTONISH.search(method):
        return "action"                               # workflow steps: confirm, validate, post, ...
    return "method"


def _rpc_kind(channel, t):
    return "%s.%s" % (channel, t) if t in ("read", "write", "onchange", "action", "method") else t


LANG_PREFIX = re.compile(r"^/[a-z]{2}(?:_[A-Z]{2})?(?=/)")
PROBE = re.compile(r"(^|/)(\.env|\.git|\.svn|\.aws|\.ssh|\.npmrc|\.htpasswd|\.pgpass|wp-|"
                   r"phpmyadmin|phpinfo|\.well-known|credentials|secrets?)|"
                   r"\.(php|zip|sql|tar|tgz|gz|bak|rar|7z|dump|tfstate|pem|jar|war|prisma|"
                   r"ya?ml|ini|conf|log)$", re.I)
STATIC_EXT = re.compile(r"\.(js|css|map|png|jpe?g|svg|gif|ico|woff2?|ttf|eot|webp|avif|mp4|webm)$", re.I)

# URL prefixes of Odoo's own controllers whose URL is not the module name.
# Anything not listed falls back to "the first path segment is the module".
URL_APP = {
    "shop": ("website_sale", "website"), "cart": ("website_sale", "website"),
    "my": ("portal", "website"), "payment": ("payment", "website"),
    "blog": ("website_blog", "website"), "forum": ("website_forum", "website"),
    "slides": ("website_slides", "website"), "event": ("website_event", "website"),
    "events": ("website_event", "website"), "jobs": ("website_hr_recruitment", "website"),
    "helpdesk": ("website_helpdesk", "website"), "appointment": ("appointment", "website"),
    "survey": ("survey", "website"), "contactus": ("website", "website"), "aboutus": ("website", "website"),
    "website": ("website", "website"), "page": ("website", "website"),
    "livechat": ("im_livechat", "messaging"), "im_livechat": ("im_livechat", "messaging"),
    "longpolling": ("bus", "messaging"), "websocket": ("bus", "messaging"), "bus": ("bus", "messaging"),
    "mail": ("mail", "messaging"), "discuss": ("mail", "messaging"),
    "pos": ("point_of_sale", "controller"), "pos-self": ("point_of_sale", "controller"),
    "pos_self_order": ("point_of_sale", "controller"), "pos-self-order": ("point_of_sale", "controller"),
    "saas_worker": ("hosting", "internal"), "_odoo": ("hosting", "internal"),
    "healthz": ("hosting", "internal"), "monitoring": ("hosting", "internal"),
}

_TOKEN = re.compile(r"^(?:[0-9a-f]{8,}|[A-Za-z0-9_\-]{24,})$")
_SLUG_ID = re.compile(r"^(.+?)-\d+$")


def _seg(s):
    if s.isdigit():
        return "<id>"
    if _TOKEN.match(s) and any(c.isdigit() for c in s):
        return "<token>"
    if _SLUG_ID.match(s):
        return "<slug>"
    return s


def _pattern(segs, depth=4):
    """['shop','my-chair-12'] -> '/shop/<slug>'. Ids, tokens and slugs collapse; deep paths are cut."""
    out = [_seg(s) for s in segs[:depth]]
    return "/" + "/".join(out) + ("/…" if len(segs) > depth else "")


def _module(seg):
    return seg.replace("-", "_") or "unknown"


def classify(method, path, status=""):
    """Return (type, app, operation). Never raises, never returns None."""
    p = path.split("?", 1)[0]
    frag = ""
    if "#" in p:
        p, _, frag = p.partition("#")

    # Odoo prefixes website URLs with the language code (/en_US/shop/...). Strip it so
    # the same page does not split into one bucket per installed language.
    lang = LANG_PREFIX.match(p)
    if lang and not p.startswith(("/web/", "/xmlrpc", "/jsonrpc", "/json/")):
        p = p[lang.end():] or "/"

    # Vulnerability scanners generate a long tail of junk paths.
    if PROBE.search(p):
        return "probe", "other", "probe %s" % (p.rsplit("/", 1)[-1] or p)[:30]

    segs = [s for s in p.split("/") if s]
    first = segs[0] if segs else ""
    tail = segs[-1] if segs else ""
    verb = "" if method in ("GET", "HEAD") else method + " "

    # ---- external API: the model and method travel after '#' in the logged path ------ #
    if first == "xmlrpc":
        svc = segs[-1] if len(segs) > 1 else ""
        if svc == "common":
            return "api.auth", "base", "xmlrpc %s" % (frag or "common")
        if svc == "db":
            return "api.method", "base", "xmlrpc db.%s" % (frag.split(".")[-1] or "?")
        if frag:
            model, meth = _split_model_method(frag)
            return _rpc_kind("api", _rpc_type(model, meth)), app_of_model(model, meth), "xmlrpc %s.%s" % (model, meth)
        return "api.method", "api", "xmlrpc %s" % (svc or "?")
    if first == "jsonrpc":
        if frag:
            model, meth = _split_model_method(frag)
            return _rpc_kind("api", _rpc_type(model, meth)), app_of_model(model, meth), "jsonrpc %s.%s" % (model, meth)
        return "api.method", "api", "jsonrpc"
    if first == "json":                                # Odoo 19 JSON-2: /json/2/<model>/<method>
        if len(segs) >= 4:
            return (_rpc_kind("api", _rpc_type(segs[2], segs[3])), app_of_model(segs[2], segs[3]),
                    "json2 %s.%s" % (segs[2], segs[3]))
        return "api.method", "api", "json2 %s" % _pattern(segs)

    # ---- the web client's RPC ----------------------------------------------------- #
    for prefix, name in (("/web/dataset/call_kw/", "call_kw"), ("/web/dataset/call_button/", "call_button")):
        if p.startswith(prefix):
            bits = p[len(prefix):].split("/")
            model = bits[0] if bits else "?"
            meth = bits[1] if len(bits) > 1 else ""
            if frag:                                   # Odoo appends #model.method
                fmodel, fmeth = _split_model_method(frag)
                model, meth = fmodel or model, fmeth or meth
            t = _rpc_type(model, meth, button=name == "call_button")
            return _rpc_kind("ui", t), app_of_model(model, meth), "%s %s.%s" % (name, model, meth)
    if p.startswith("/web/dataset/"):
        what = tail
        return ("ui.write" if what == "resequence" else "ui.read" if what == "search_read" else "ui.method",
                "web", "dataset %s" % what)

    # ---- sessions and authentication --------------------------------------------- #
    if p in ("/web/session/authenticate", "/web/session/impersonate") or p.startswith(
            ("/web/login", "/web/signup", "/web/reset_password")):
        return "auth", "web", "login %s" % p
    if p in ("/web/session/logout", "/web/session/destroy"):
        return "auth", "web", "logout"
    if first in ("auth_oauth", "auth_totp", "auth_signup", "auth_passkey"):
        return "auth", first, "login %s" % _pattern(segs, 2)

    # ---- web client shell, actions, menus ------------------------------------------ #
    if p == "/web/action/run":
        return "ui.action", "web", "run server action (/web/action/run)"
    if p.startswith("/web/action/"):
        return "ui.load", "web", "load action (/web/action/%s)" % tail
    if p.startswith(("/web/webclient/", "/web/session/", "/web/bundle", "/web/tests", "/web/domain/")):
        return "ui.load", "web", "webclient %s" % tail
    if p == "/odoo" or p.startswith("/odoo/"):
        return "ui.load", "web", "client shell /odoo/*"
    if p == "/web" or p.startswith("/web#"):
        return "ui.load", "web", "client shell /web"

    # ---- import / export -------------------------------------------------------------- #
    if first == "base_import" or "base_import" in frag:
        return "import", "base", "base_import %s" % (frag or p).rsplit(".", 1)[-1].rsplit("/", 1)[-1]
    if p.startswith("/web/export/") or p.endswith("/export_xlsx") or "/export/" in p:
        return "export", "web", "export %s" % tail

    # ---- reports: /report/<pdf|html|text>/<module.report_name>/<ids> ------------------- #
    if first == "report":
        kind = segs[1] if len(segs) > 1 else "?"
        name = segs[2] if len(segs) > 2 else "*"
        return "report", (name.split(".")[0] if "." in name else "report"), "report %s %s" % (kind, name)
    if first == "account_reports" or "account_report" in frag:
        return "report", "account_reports", "account report %s" % (frag or p)

    # ---- attachments, images, bundles, static files -------------------------------------- #
    if p.startswith("/web/assets"):
        return "assets", "web", "bundle %s" % tail
    if p.startswith(("/web/image", "/web/content", "/web/binary")):
        what = first and segs[1] if len(segs) > 1 else "content"
        if len(segs) > 3 and "." in segs[2] and segs[3].isdigit():
            model = segs[2]                                # /web/image/<model>/<id>/<field>
            field = segs[4] if len(segs) > 4 else ""
            return "assets", app_of_model(model), "%s %s%s" % (what, model, "." + field if field else "")
        return "assets", "web", "%s (attachment)" % what
    if STATIC_EXT.search(p) or p.startswith(("/favicon", "/robots.txt", "/sitemap")) or "/static/" in p:
        return "assets", (_module(first) if len(segs) > 1 else "website"), "static %s" % (first if len(segs) > 1 else tail)

    # ---- everything owned by a URL prefix ------------------------------------------------ #
    if first in URL_APP:
        app, kind = URL_APP[first]
        if kind == "website" and status == "404":
            kind = "not-found"
        return kind, app, verb + _pattern(segs)

    # ---- unknown routes stay attributable ------------------------------------------------ #
    if len(segs) >= 2:
        kind = "not-found" if status == "404" else "controller"
        return kind, _module(first), verb + _pattern(segs)
    if not segs or len(segs) == 1:                          # "/" or a single-segment CMS page slug
        if status == "404":
            return "not-found", "website", "%spage /*" % verb
        return "website", "website", "%spage %s" % (verb, "/" if not segs else "/*")
    return "other", "other", "%s %s" % (method, _pattern(segs))


LOGGER_FAMILIES = (
    ("odoo.addons.base.models.res_users", "auth"),
    ("odoo.addons.base.models.ir_rule", "access-rules"),
    ("odoo.addons.base.models.ir_cron", "cron"),
    ("odoo.addons.base.models.ir_model", "orm-schema"),
    ("odoo.addons.base.models.ir_attachment", "attachments"),
    ("odoo.addons.base.models.ir_actions_report", "report-engine"),
    ("odoo.addons.base_automation", "automation-rules"),
    ("odoo.addons.mail", "mail"),
    ("odoo.addons.bus", "bus"),
    ("odoo.addons.saas_worker", "hosting-internal"),
    ("odoo.addons.iap", "integration"),
    ("odoo.modules", "module-loading"),
    ("odoo.registry", "registry"),
    ("odoo.schema", "orm-schema"),
    ("odoo.api", "orm-recompute"),
    ("odoo.fields", "orm-fields"),
    ("odoo.domains", "orm-domains"),
    ("odoo.sql_db", "database"),
    ("odoo.models", "orm"),
    ("odoo.http", "http-errors"),
    ("py.warnings", "deprecations"),
    ("longpolling", "bus"),
    ("werkzeug", "requests"),
)


def event_family(logger):
    for prefix, fam in LOGGER_FAMILIES:
        if logger == prefix or logger.startswith(prefix + "."):
            return fam
    if logger.startswith("odoo.addons."):
        return "addon:" + logger.split(".")[2]
    return logger.split(".")[0] or "unknown"
