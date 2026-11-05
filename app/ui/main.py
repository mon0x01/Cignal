from __future__ import annotations

from nicegui import app, events, ui

from app.core.config import settings
from app.ui.api_client import get, patch, post, request
from app.ui.components.layout import require_user, shell

STATIC_DIR = __import__("pathlib").Path(__file__).parent / "static"
app.add_static_files("/static", str(STATIC_DIR))
app.add_static_file(local_file=STATIC_DIR / "manifest.webmanifest", url_path="/manifest.webmanifest")
app.add_static_file(local_file=STATIC_DIR / "service-worker.js", url_path="/service-worker.js")
app.add_static_file(local_file=STATIC_DIR / "register-sw.js", url_path="/register-sw.js")
app.add_static_file(local_file=STATIC_DIR / "push-client.js", url_path="/push-client.js")
app.add_static_file(local_file=STATIC_DIR / "offline-draft.js", url_path="/offline-draft.js")


def _notify_error(exc: Exception):
    ui.notify(str(exc), type="negative", position="top")


def _card_title(title: str, subtitle: str | None = None):
    with ui.column().classes("gap-1"):
        ui.label(title).classes("text-base font-bold text-slate-100")
        if subtitle:
            ui.label(subtitle).classes("text-xs text-slate-400")


def _metric(label: str, value, detail: str, icon: str, color: str = "text-orange-400"):
    with ui.card().classes("bg-slate-900 border border-slate-800 rounded-xl p-4 min-w-[180px] flex-1"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label(label.upper()).classes("text-[10px] font-bold tracking-[0.15em] text-slate-400")
            ui.icon(icon).classes(color)
        ui.label(str(value)).classes("text-3xl font-black text-slate-100 mt-2")
        ui.label(detail).classes("text-xs text-slate-500")


def _provenance_badge(value: str):
    colors = {"live": "positive", "simulated": "warning", "historical": "info", "submitted": "secondary"}
    ui.badge((value or "unknown").upper(), color=colors.get(value, "grey")).props("outline")


def _status_badge(value: str):
    colors = {"critical": "negative", "high": "negative", "moderate": "warning", "low": "positive", "verified": "positive", "submitted": "info", "analyzed": "info", "rejected": "grey", "resolved": "grey", "needs_info": "warning"}
    ui.badge((value or "unknown").replace("_", " ").upper(), color=colors.get(value, "grey")).props("outline")


def _page_error(message: str):
    with ui.card().classes("bg-slate-900 border border-slate-700 w-full"):
        ui.icon("warning").classes("text-orange-300")
        ui.label(message).classes("text-orange-300")


@ui.page("/login")
def login_page():
    ui.colors(primary="#38bdf8", dark="#08111f")
    ui.dark_mode().enable()
    ui.page_title("Secure access · Cignal")
    ui.add_head_html(f'<meta name="cignal-api-url" content="{settings.api_public_url}"><link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#08111f"><script defer src="/register-sw.js"></script><script defer src="/push-client.js"></script><script defer src="/offline-draft.js"></script>')
    setup = {}
    try:
        setup = get("/api/setup/status", authenticated=False)
    except Exception as exc:
        _page_error(f"Cannot reach Cignal API: {exc}")
        return
    with ui.row().classes("w-full min-h-screen items-center justify-center bg-slate-950 p-4"):
        with ui.card().classes("w-full max-w-md bg-slate-900 border border-slate-800 rounded-2xl p-7 shadow-xl"):
            with ui.row().classes("items-center gap-3"):
                ui.icon("radar", size="2.5rem").classes("text-orange-400")
                with ui.column().classes("gap-0"):
                    ui.label("CIGNAL").classes("text-xl font-black tracking-[0.25em] text-white")
                    ui.label("DISASTER INTELLIGENCE PLATFORM").classes("text-[9px] tracking-[0.15em] text-slate-400")
            ui.separator().classes("bg-slate-800 my-4")
            if setup.get("setup_required"):
                ui.label("Initial administrator setup").classes("text-xl font-bold text-slate-100")
                ui.label("Create the first administrator. This setup route closes after the first account is created.").classes("text-sm text-slate-400")
                name = ui.input("Display name").classes("w-full").props("outlined dark")
                email = ui.input("Administrator email").classes("w-full").props("outlined dark type=email")
                password = ui.input("Password (minimum 14 characters)", password=True, password_toggle_button=True).classes("w-full").props("outlined dark")
                confirm = ui.input("Confirm password", password=True, password_toggle_button=True).classes("w-full").props("outlined dark")
                def create_admin():
                    if password.value != confirm.value:
                        ui.notify("Passwords do not match", type="negative")
                        return
                    try:
                        result = post("/api/setup/admin", json={"display_name": name.value, "email": email.value, "password": password.value}, authenticated=False)
                        app.storage.user["access_token"] = result["access_token"]
                        app.storage.user["refresh_token"] = result["refresh_token"]
                        app.storage.user["user"] = result["user"]
                        ui.navigate.to("/mfa-setup")
                    except Exception as exc:
                        _notify_error(exc)
                ui.button("Create administrator", on_click=create_admin).props("unelevated color=primary no-caps").classes("w-full mt-3")
            else:
                ui.label("Secure sign in").classes("text-xl font-bold text-slate-100")
                ui.label("Access is role-controlled and sensitive actions are audited.").classes("text-sm text-slate-400")
                email = ui.input("Email").classes("w-full").props("outlined dark type=email")
                password = ui.input("Password", password=True, password_toggle_button=True).classes("w-full").props("outlined dark")
                totp = ui.input("Authenticator code or recovery code (when enabled)").classes("w-full").props("outlined dark")
                def sign_in():
                    try:
                        result = post("/api/auth/login", json={"email": email.value, "password": password.value, "totp_code": totp.value or None}, authenticated=False)
                        app.storage.user["access_token"] = result["access_token"]
                        app.storage.user["refresh_token"] = result["refresh_token"]
                        app.storage.user["user"] = result["user"]
                        ui.navigate.to("/mfa-setup" if result.get("mfa_setup_required") else "/")
                    except Exception as exc:
                        _notify_error(exc)
                ui.button("Sign in", on_click=sign_in).props("unelevated color=primary no-caps").classes("w-full mt-3")
                ui.label("MFA is required for all authenticated accounts. Each demo user must enroll an authenticator on first sign-in.").classes("text-xs text-orange-300 mt-3")
                ui.button("Submit a report without an account", icon="campaign", on_click=lambda: ui.navigate.to("/submit-anonymously")).props("flat color=primary no-caps").classes("w-full")
                if settings.demo_mode:
                    ui.separator().classes("bg-slate-800 my-3")
                    ui.label("DEMONSTRATION ACCESS").classes("text-[10px] font-bold tracking-[0.16em] text-slate-400")
                    ui.label("admin@cignal.demo · analyst@cignal.demo · responder@cignal.demo · citizen@cignal.demo").classes("text-xs text-slate-300")
                    ui.label("Password: Cignal-Demo-2026! (demo only; never use in production)").classes("text-xs text-orange-300")


@ui.page("/mfa-setup")
def mfa_setup_page():
    user = app.storage.user.get("user")
    token = app.storage.user.get("access_token")
    if not token or not user:
        ui.navigate.to("/login")
        return
    if user.get("mfa_enabled"):
        ui.navigate.to("/")
        return
    ui.colors(primary="#38bdf8", dark="#08111f")
    ui.dark_mode().enable()
    ui.page_title("Set up MFA · Cignal")
    with ui.row().classes("w-full min-h-screen items-center justify-center bg-slate-950 p-4"):
        with ui.card().classes("w-full max-w-xl bg-slate-900 border border-slate-800 p-7"):
            ui.icon("shield_lock", size="2.5rem").classes("text-orange-400")
            ui.label("Enable multi-factor authentication").classes("text-2xl font-bold text-white")
            ui.label("Use an authenticator app such as Aegis, 2FAS, Google Authenticator or Microsoft Authenticator.").classes("text-sm text-slate-400")
            secret_label = ui.label("Generate a setup secret to begin.").classes("font-mono text-sm text-orange-300 break-all")
            uri_label = ui.label("").classes("text-xs text-slate-500 break-all")
            code = ui.input("6-digit authenticator code").props("outlined dark inputmode=numeric maxlength=6").classes("w-full")
            recovery = ui.column().classes("w-full")
            started = {"value": False}
            def begin():
                try:
                    result = post("/api/auth/mfa/start")
                    secret_label.set_text(f"Secret: {result['secret']}")
                    uri_label.set_text(result["provisioning_uri"])
                    started["value"] = True
                except Exception as exc:
                    _notify_error(exc)
            def verify():
                if not started["value"]:
                    ui.notify("Generate the MFA secret first", type="warning")
                    return
                try:
                    result = post("/api/auth/mfa/verify", json={"code": code.value})
                    user["mfa_enabled"] = True
                    app.storage.user["user"] = user
                    with recovery:
                        ui.label("Save these recovery codes offline. They are displayed once.").classes("text-orange-300 text-sm font-bold")
                        ui.code("\n".join(result["recovery_codes"])).classes("w-full")
                        ui.button("I saved them — continue", on_click=lambda: ui.navigate.to("/")).props("color=primary unelevated")
                except Exception as exc:
                    _notify_error(exc)
            ui.button("Generate authenticator secret", on_click=begin).props("outline color=primary no-caps")
            ui.button("Verify and enable MFA", on_click=verify).props("unelevated color=primary no-caps").classes("w-full mt-2")


@ui.page("/")
def dashboard_page():
    user = require_user()
    if not user:
        return
    shell("Dashboard", user)
    try:
        metrics = get("/api/dashboard/summary")
        if user["role"] == "citizen":
            with ui.row().classes("w-full flex-wrap gap-3"):
                _metric("My reports", metrics["my_reports"], "All submissions", "description")
                _metric("Awaiting review", metrics["pending"], "Not yet verified", "hourglass_top", "text-orange-400")
                _metric("Verified", metrics["verified"], "Linked to verified incidents", "verified_user", "text-orange-300")
            with ui.card().classes("bg-slate-900 border border-slate-800 w-full"):
                _card_title("Your next action", "Reports are reviewed by human analysts. AI is advisory only.")
                ui.button("Submit a disaster report", icon="add_location_alt", on_click=lambda: ui.navigate.to("/submit")).props("unelevated color=primary no-caps")
            return
        with ui.row().classes("w-full flex-wrap gap-3"):
            _metric("Active incidents", metrics["active_incidents"], "Verified or monitoring", "crisis_alert", "text-orange-300")
            _metric("Critical", metrics["critical_incidents"], "Requires attention", "priority_high", "text-orange-300")
            _metric("Pending reports", metrics["pending_reports"], "Awaiting analyst decision", "fact_check", "text-orange-400")
            _metric("Reports / 24h", metrics["reports_24h"], "Incoming signal volume", "query_stats")
            _metric("Configured live sources", metrics["sources_live"], "Ready connectors; not all require keys", "hub", "text-orange-300")
        with ui.row().classes("w-full items-stretch gap-4 flex-wrap"):
            with ui.card().classes("bg-slate-900 border border-slate-800 flex-1 min-w-[320px]"):
                _card_title("Operational map", "Verified incident locations · OpenStreetMap tiles")
                incidents = get("/api/incidents", params={"limit": 100})
                map_view = ui.leaflet(center=(23.2, 72.4), zoom=6).classes("w-full h-80 rounded-lg overflow-hidden")
                for incident in incidents:
                    if incident.get("latitude") is not None and incident.get("longitude") is not None:
                        popup = f"{incident['incident_code']} · {incident['title']} · {incident['severity']}"
                        map_view.marker(latlng=(incident["latitude"], incident["longitude"]), options={"title": popup})
                ui.label("Map pins show Cignal incident records, not official responder dispatch locations.").classes("text-[10px] text-slate-500")
            with ui.card().classes("bg-slate-900 border border-slate-800 flex-1 min-w-[280px]"):
                _card_title("Latest verified incidents", "Most recently updated")
                for incident in incidents[:5]:
                    with ui.row().classes("w-full items-start justify-between border-b border-slate-800 py-2"):
                        with ui.column().classes("gap-1 flex-1"):
                            ui.label(incident["title"]).classes("text-sm text-slate-100 font-semibold")
                            ui.label(incident.get("location_text") or "Location not resolved").classes("text-xs text-slate-400")
                        _status_badge(incident["severity"])
                ui.button("Open incident monitor", on_click=lambda: ui.navigate.to("/incidents")).props("flat color=primary no-caps")
        with ui.card().classes("bg-slate-900 border border-slate-800 w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                _card_title("Decision-support guardrail", "The system does not autonomously declare emergencies or dispatch responders.")
                ui.badge("HUMAN VERIFICATION REQUIRED", color="warning").props("outline")
            ui.label("Submitted report → AI triage hint → candidate duplicate links → analyst review → verified incident → responder visibility").classes("text-sm text-slate-300")
    except Exception as exc:
        _page_error(str(exc))


def _render_incident_activities(container, activities):
    container.clear()
    with container:
        if not activities:
            ui.label("No operational updates have been recorded.").classes("text-sm text-slate-400")
        for activity in activities:
            with ui.card().classes("w-full bg-slate-950 border border-slate-800 p-3"):
                with ui.row().classes("w-full items-center justify-between flex-wrap"):
                    ui.badge(activity["activity_type"].replace("_", " ").upper(), color="info").props("outline")
                    ui.label(f"{activity['actor_display_name']} · {activity['actor_role'].upper()}").classes("text-xs text-slate-400")
                ui.label(activity["message"]).classes("text-sm text-slate-200 whitespace-pre-wrap")
                if activity.get("location_text"):
                    ui.label(f"Location: {activity['location_text']}").classes("text-xs text-slate-400")
                ui.label(activity["created_at"]).classes("text-[10px] text-slate-500")


def show_incident_details(item):
    try:
        detail = get(f"/api/incidents/{item['id']}")
        activity_page = get(f"/api/incidents/{item['id']}/activity", params={"limit": 50, "offset": 0})
        dialog = ui.dialog()
        with dialog:
            with ui.card().classes("w-[min(900px,95vw)] max-w-5xl bg-slate-900 border border-slate-700 p-5"):
                with ui.row().classes("w-full items-start justify-between"):
                    with ui.column().classes("gap-1"):
                        ui.label(detail["incident_code"]).classes("font-mono text-xs text-orange-300")
                        ui.label(detail["title"]).classes("text-xl font-bold text-slate-100")
                        ui.label(f"{detail['disaster_type']} · {detail['severity']} · {detail['status']}").classes("text-xs text-slate-400")
                    ui.button(icon="close", on_click=dialog.close).props("flat round dense")
                ui.label(detail.get("summary") or "No incident summary recorded.").classes("text-sm text-slate-300 whitespace-pre-wrap")
                ui.label(
                    f"Location: {detail.get('location_text') or 'Not resolved'} · "
                    f"Linked reports: {detail.get('source_count', 0)} · "
                    f"Confidence: {detail.get('confidence', 0):.0%}"
                ).classes("text-xs text-slate-400")
                ui.separator().classes("bg-slate-700")
                ui.label("Operational activity timeline").classes("text-base font-bold text-slate-100")
                ui.label("Responder observations and support requests are operational notes; they do not change incident status.").classes("text-xs text-slate-400")
                activities = list(activity_page.get("items", []))
                activity_cursor = {"value": activity_page.get("next_cursor")}
                activity_container = ui.column().classes("w-full gap-2")
                _render_incident_activities(activity_container, activities)
                if activity_page.get("has_more"):
                    activity_count_label = ui.label(
                        f"Showing {len(activities)} of {activity_page['total']} updates."
                    ).classes("text-xs text-slate-500")
                    def load_older_activities():
                        try:
                            cursor = activity_cursor["value"]
                            if not cursor:
                                load_older.set_visibility(False)
                                return
                            next_page = get(
                                f"/api/incidents/{item['id']}/activity",
                                params={"limit": 50, **cursor},
                            )
                            activities.extend(next_page.get("items", []))
                            activity_cursor["value"] = next_page.get("next_cursor")
                            _render_incident_activities(activity_container, activities)
                            activity_count_label.set_text(
                                f"Showing {len(activities)} of {next_page['total']} updates."
                            )
                            if not next_page.get("has_more"):
                                load_older.set_visibility(False)
                        except Exception as exc:
                            _notify_error(exc)
                    load_older = ui.button(
                        "Load older updates", icon="history", on_click=load_older_activities
                    ).props("flat color=primary no-caps")
                if detail["status"] != "closed":
                    ui.separator().classes("bg-slate-700")
                    ui.label("Add operational update").classes("text-base font-bold text-slate-100")
                    activity_type = ui.select(
                        {
                            "situation_update": "Situation update",
                            "observation": "Field observation",
                            "resource_request": "Resource request",
                            "access_constraint": "Access constraint",
                            "handover": "Shift handover",
                            "note": "General note",
                        },
                        value="situation_update", label="Update type",
                    ).classes("w-full")
                    activity_message = ui.textarea("What changed? Include source and time context where known.").props(
                        "outlined dark autogrow maxlength=3000"
                    ).classes("w-full")
                    activity_location = ui.input("Location (optional)").props("outlined dark maxlength=220").classes("w-full")
                    def submit_activity():
                        try:
                            post(f"/api/incidents/{item['id']}/activity", json={
                                "activity_type": activity_type.value,
                                "message": activity_message.value or "",
                                "location_text": activity_location.value or None,
                            })
                            ui.notify("Operational update recorded.", type="positive")
                            dialog.close()
                            show_incident_details(item)
                        except Exception as exc:
                            _notify_error(exc)
                    ui.button("Record update", icon="add_comment", on_click=submit_activity).props("unelevated color=primary no-caps")
                else:
                    ui.label("This incident is closed. The activity timeline is read-only.").classes("text-xs text-orange-300")
                ui.separator().classes("bg-slate-700")
                ui.label("Supporting reports").classes("text-base font-bold text-slate-100")
                links = detail.get("report_links", [])
                if not links:
                    ui.label("No supporting reports are linked to this incident.").classes("text-sm text-slate-400")
                for link in links:
                    report = link["report"]
                    with ui.card().classes("w-full bg-slate-950 border border-slate-800"):
                        with ui.row().classes("w-full items-center justify-between flex-wrap"):
                            ui.label(report["tracking_code"]).classes("font-mono text-xs text-orange-300")
                            _status_badge(report["status"])
                        ui.label(report["title"]).classes("font-semibold text-slate-100")
                        ui.label(report["description"]).classes("text-sm text-slate-300 whitespace-pre-wrap")
                        ui.label(
                            f"Source: {report['source_name']} · Provenance: {report['provenance']} · "
                            f"Relationship: {link['relationship_type']} · Evidence items: {link['evidence_count']}"
                        ).classes("text-xs text-slate-500")
                        ui.label(f"Linked: {link['linked_at']}").classes("text-[10px] text-slate-500")
                ui.button("Close", on_click=dialog.close).props("outline color=primary no-caps")
        dialog.open()
    except Exception as exc:
        _notify_error(exc)


def set_incident_status(item, new_status):
    try:
        patch(f"/api/incidents/{item['id']}", json={"status": new_status})
        ui.notify(f"Incident moved to {new_status.replace('_', ' ')}", type="positive")
        ui.navigate.to("/incidents")
    except Exception as exc:
        _notify_error(exc)


@ui.page("/incidents")
def incidents_page():
    user = require_user()
    if not user:
        return
    shell("Incident monitor", user)
    try:
        incidents = get("/api/incidents", params={"limit": 200})
        with ui.row().classes("w-full flex-wrap gap-2"):
            container = ui.column().classes("w-full gap-3")
            severity = ui.select(["all", "critical", "high", "moderate", "low"], value="all", label="Severity").classes("w-48")
            status = ui.select(["all", "verified", "monitoring", "resolved", "closed"], value="all", label="Status").classes("w-48")
            def render():
                container.clear()
                selected = [x for x in incidents if (severity.value == "all" or x["severity"] == severity.value) and (status.value == "all" or x["status"] == status.value)]
                with container:
                    if not selected:
                        ui.label("No incidents match these filters.").classes("text-slate-400")
                    for item in selected:
                        with ui.card().classes("bg-slate-900 border border-slate-800 w-full"):
                            with ui.row().classes("w-full items-start justify-between flex-wrap"):
                                with ui.column().classes("gap-1 flex-1"):
                                    with ui.row().classes("items-center gap-2"):
                                        ui.label(item["incident_code"]).classes("font-mono text-xs text-orange-300")
                                        _status_badge(item["severity"])
                                        _status_badge(item["status"])
                                        _provenance_badge(item["provenance"])
                                    ui.label(item["title"]).classes("text-lg font-bold text-slate-100")
                                    ui.label(item.get("location_text") or "Location not resolved").classes("text-xs text-slate-400")
                                    ui.label(item["summary"]).classes("text-sm text-slate-300")
                                    ui.label(f"Linked report count: {item['source_count']} · Confidence: {item['confidence']:.0%}").classes("text-xs text-slate-500")
                                with ui.column().classes("gap-1"):
                                    ui.button("View incident details & activity", icon="fact_check", on_click=lambda x=item: show_incident_details(x)).props("outline color=primary no-caps")
                                    if user["role"] in ("admin", "analyst"):
                                        if item["status"] == "verified":
                                            ui.button("Start monitoring", icon="visibility", on_click=lambda x=item: set_incident_status(x, "monitoring")).props("outline color=primary no-caps")
                                        if item["status"] in ("verified", "monitoring"):
                                            ui.button("Resolve", icon="check_circle", on_click=lambda x=item: set_incident_status(x, "resolved")).props("outline color=positive no-caps")
                                        if item["status"] == "resolved":
                                            ui.button("Close case", icon="lock", on_click=lambda x=item: set_incident_status(x, "closed")).props("outline color=secondary no-caps")
            severity.on("update:model-value", lambda _: render())
            status.on("update:model-value", lambda _: render())
            render()
    except Exception as exc:
        _page_error(str(exc))


def link_report_to_incident(report):
    try:
        incidents = get("/api/incidents", params={"limit": 200})
        open_incidents = [item for item in incidents if item["status"] in ("verified", "monitoring")]
        if not open_incidents:
            ui.notify("There are no verified or monitoring incidents available to link.", type="warning")
            return
        # NiceGUI select dictionaries map option values to display labels.
        options = {
            item["id"]: f"{item['incident_code']} · {item['title']} ({item['status']}, {item['severity']})"
            for item in open_incidents
        }
        dialog = ui.dialog()
        with dialog:
            with ui.card().classes("w-[min(640px,95vw)] bg-slate-900 border border-slate-700 p-5"):
                ui.label("Link report to an existing incident").classes("text-lg font-bold text-slate-100")
                ui.label(report["title"]).classes("text-sm text-slate-300")
                target = ui.select(options, label="Verified / monitoring incident", value=None).classes("w-full")
                note = ui.textarea("Analyst note (optional)").classes("w-full").props("outlined dark autogrow maxlength=1000")
                def confirm_link():
                    if not target.value:
                        ui.notify("Select an incident first.", type="warning")
                        return
                    try:
                        post(
                            f"/api/reports/{report['id']}/review",
                            json={"decision": "link", "incident_id": target.value, "note": note.value},
                        )
                        ui.notify("Report linked to incident. Source count and audit history updated.", type="positive")
                        dialog.close()
                        ui.navigate.to("/reports")
                    except Exception as exc:
                        _notify_error(exc)
                with ui.row().classes("w-full justify-end"):
                    ui.button("Cancel", on_click=dialog.close).props("flat color=secondary no-caps")
                    ui.button("Link report", icon="link", on_click=confirm_link).props("unelevated color=primary no-caps")
        dialog.open()
    except Exception as exc:
        _notify_error(exc)


def review_with_note(report, decision):
    action_title = "Request additional information" if decision == "request_info" else "Reject report"
    dialog = ui.dialog()
    with dialog:
        with ui.card().classes("w-[min(600px,95vw)] bg-slate-900 border border-slate-700 p-5"):
            ui.label(action_title).classes("text-lg font-bold text-slate-100")
            ui.label(report["title"]).classes("text-sm text-slate-300")
            note = ui.textarea("Reason / message to submitter (optional)").classes("w-full").props("outlined dark autogrow maxlength=1000")
            def confirm_review():
                try:
                    post(f"/api/reports/{report['id']}/review", json={"decision": decision, "note": note.value})
                    ui.notify("Report decision saved.", type="positive")
                    dialog.close()
                    ui.navigate.to("/reports")
                except Exception as exc:
                    _notify_error(exc)
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=dialog.close).props("flat color=secondary no-caps")
                ui.button("Confirm", on_click=confirm_review).props("unelevated color=negative no-caps" if decision == "reject" else "unelevated color=warning no-caps")
    dialog.open()


@ui.page("/reports")
def reports_page():
    user = require_user()
    if not user:
        return
    shell("Report review" if user["role"] != "citizen" else "My reports", user)
    if user["role"] == "responder":
        ui.label("Responders access verified incident records, not the unverified report inbox.").classes("text-sm text-slate-400")
        ui.button("Open incident monitor", on_click=lambda: ui.navigate.to("/incidents")).props("unelevated color=primary no-caps")
        return

    ui.label(
        "Search the complete report history. Anonymous submissions are visible to analysts and administrators; citizen accounts see only their own reports."
    ).classes("text-sm text-slate-400")

    with ui.card().classes("bg-slate-900 border border-slate-800 w-full"):
        search = ui.input(
            "Search reports",
            placeholder="Tracking code, title, description, source or location",
        ).props("outlined dark clearable").classes("w-full")
        with ui.row().classes("w-full flex-wrap items-end gap-3"):
            status_filter = ui.select(
                {
                    "all": "All statuses",
                    "submitted": "Submitted",
                    "analyzed": "Analyzed",
                    "needs_info": "Needs information",
                    "verified": "Verified",
                    "rejected": "Rejected",
                },
                value="all",
                label="Status",
            ).classes("min-w-[170px] flex-1")
            source_filter = ui.select(
                {
                    "all": "All sources",
                    "citizen": "Citizen",
                    "usgs": "USGS",
                    "gdacs": "GDACS",
                    "open_meteo": "Open-Meteo",
                    "nasa_firms": "NASA FIRMS",
                    "imd": "IMD",
                    "india_gov": "India government",
                    "bhuvan": "Bhuvan",
                    "authorized_rss": "Authorized RSS",
                    "social_api": "Authorized social API",
                },
                value="all",
                label="Source",
            ).classes("min-w-[170px] flex-1")
            disaster_filter = ui.select(
                {
                    "all": "All disaster types",
                    "unknown": "Unknown",
                    "earthquake": "Earthquake",
                    "flood": "Flood",
                    "fire": "Fire",
                    "cyclone": "Cyclone",
                    "landslide": "Landslide",
                    "other": "Other",
                },
                value="all",
                label="Disaster type",
            ).classes("min-w-[170px] flex-1")
        with ui.row().classes("w-full flex-wrap items-end gap-3"):
            date_from = ui.input("From date (UTC)", placeholder="YYYY-MM-DD").props("outlined dark clearable").classes("min-w-[150px] flex-1")
            date_to = ui.input("To date (UTC)", placeholder="YYYY-MM-DD").props("outlined dark clearable").classes("min-w-[150px] flex-1")
            sort = ui.select(
                {"newest": "Newest first", "oldest": "Oldest first", "urgency": "Highest urgency"},
                value="newest",
                label="Sort",
            ).classes("min-w-[160px] flex-1")
            page_size = ui.select({"25": "25 / page", "50": "50 / page", "100": "100 / page"}, value="25", label="Page size").classes("min-w-[125px]")
        with ui.row().classes("w-full justify-end"):
            ui.button("Clear filters", on_click=lambda: clear_filters()).props("flat color=secondary no-caps")
            ui.button("Apply filters", icon="search", on_click=lambda: apply_filters()).props("unelevated color=primary no-caps")

    page_state = {"page": 1}
    cards = ui.column().classes("w-full gap-3")
    with ui.row().classes("w-full items-center justify-between flex-wrap"):
        result_summary = ui.label("Loading reports…").classes("text-sm text-slate-400")
        with ui.row().classes("items-center gap-2"):
            previous_button = ui.button(
                "Previous", icon="chevron_left", on_click=lambda: load_page(page_state["page"] - 1)
            ).props("outline color=secondary no-caps")
            page_summary = ui.label("").classes("text-xs text-slate-400")
            next_button = ui.button(
                "Next", icon="chevron_right", on_click=lambda: load_page(page_state["page"] + 1)
            ).props("outline color=secondary no-caps")

    def show_ai_analysis(report):
        try:
            payload = get(f"/api/reports/{report['id']}/analysis")
            analysis = payload["analysis"]
            text_analysis = analysis.get("text", {})
            fusion = analysis.get("fusion", {})
            vision = analysis.get("vision", {})
            dialog = ui.dialog()
            with dialog, ui.card().classes("bg-slate-900 border border-slate-800 w-full max-w-2xl"):  # details on demand; dashboard stays compact
                ui.label("AI assessment").classes("text-lg font-bold text-slate-100")
                ui.label(analysis.get("summary", "No summary available.")).classes("text-sm text-slate-300")
                with ui.row().classes("w-full flex-wrap gap-2"):
                    ui.badge(f"Category: {fusion.get('category', 'unknown')}").props("outline")
                    ui.badge(f"Urgency: {fusion.get('urgency', 'unknown')}").props("outline")
                    ui.badge(f"Text score: {float(fusion.get('confidence') or 0):.0%}").props("outline")
                ui.label(
                    f"Model: {text_analysis.get('method', 'unknown')} · Version: {text_analysis.get('model_version', 'unknown')}"
                ).classes("text-xs text-slate-400")
                ui.label(
                    f"Evidence analyzed: {vision.get('images_analyzed', 0)} images · "
                    f"Failed: {vision.get('images_failed', 0)} · Skipped by limit: {vision.get('images_skipped_limit', 0)}"
                ).classes("text-xs text-slate-400")
                visual_candidates = fusion.get("visual_candidates", [])
                if visual_candidates:
                    ui.label("Image model candidates · human review required").classes("text-sm font-semibold text-slate-200")
                    for item in visual_candidates[:5]:
                        ui.label(
                            f"{item.get('label')} · {item.get('image_count')} image(s) · "
                            f"candidate score {item.get('mean_candidate_score', 0):.2f}"
                        ).classes("text-xs text-slate-300")
                warnings = vision.get("quality_warnings", [])
                if warnings:
                    ui.label("Image quality flags").classes("text-sm font-semibold text-slate-200")
                    for item in warnings[:8]:
                        ui.label(f"{item.get('evidence_id')}: {item.get('flag')}").classes("text-xs text-orange-300")
                conflicts = fusion.get("conflicts", [])
                if conflicts:
                    ui.label("Location conflicts require analyst review").classes("text-sm font-semibold text-orange-300")
                    for item in conflicts[:4]:
                        ui.label(f"{item.get('type')} · {item.get('distance_km')} km").classes("text-xs text-slate-300")
                candidates = analysis.get("duplicate_candidates", [])
                if candidates:
                    ui.label("Related report candidates").classes("text-sm font-semibold text-slate-200")
                    for item in candidates[:5]:
                        ui.label(
                            f"{item.get('candidate_id')} · score {item.get('score', 0):.2f} · "
                            f"text {item.get('text_similarity', 0):.2f}"
                        ).classes("text-xs text-slate-300")
                ui.label("Advisory output only. It does not verify incidents or dispatch responders.").classes("text-xs text-orange-300")
                ui.button("Close", on_click=dialog.close).props("outline color=primary no-caps")
            dialog.open()
        except Exception as exc:
            _notify_error(exc)

    def render_report(report):
        with ui.card().classes("bg-slate-900 border border-slate-800 w-full"):
            with ui.row().classes("w-full items-start justify-between flex-wrap"):
                with ui.column().classes("gap-1 flex-1 min-w-[240px]"):
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.label(report["tracking_code"]).classes("font-mono text-xs text-orange-300")
                        _status_badge(report["status"])
                        _status_badge(report["urgency"])
                        _provenance_badge(report["provenance"])
                    ui.label(report["title"]).classes("text-lg font-bold text-slate-100")
                    ui.label(report.get("location_text") or "Location not resolved").classes("text-xs text-slate-400")
                    ui.label(report["description"]).classes("text-sm text-slate-300")
                    if report.get("ai_summary"):
                        ui.label(
                            f"AI triage · {report['ai_summary']} · confidence {report.get('ai_confidence') or 0:.0%}"
                        ).classes("text-xs text-orange-300")
                        if user["role"] in ("admin", "analyst"):
                            ui.button(
                                "AI assessment details", icon="analytics",
                                on_click=lambda r=report: show_ai_analysis(r),
                            ).props("flat dense color=primary no-caps")
                    if report.get("duplicate_of"):
                        ui.label(
                            f"Possible duplicate candidate: {report['duplicate_of']} (analyst decision required)"
                        ).classes("text-xs text-orange-300")
                    ui.label(
                        f"Source: {report['source_name']} · Language: {report['language']} · "
                        f"Submitted: {report['created_at']}"
                    ).classes("text-[10px] text-slate-500")
                    if user["role"] == "citizen" and report["status"] == "needs_info":
                        followup = ui.textarea(
                            "Additional information requested by the analyst (20–3000 characters)"
                        ).classes("w-full").props("outlined dark autogrow maxlength=3000")

                        def send_followup(r=report, field=followup):
                            try:
                                post(f"/api/reports/{r['id']}/followup", json={"message": field.value})
                                ui.notify("Additional information submitted for review", type="positive")
                                ui.navigate.to("/reports")
                            except Exception as exc:
                                _notify_error(exc)

                        ui.button("Send additional information", on_click=send_followup).props("outline color=primary no-caps")
                if user["role"] in ("admin", "analyst"):
                    with ui.column().classes("gap-2 min-w-[190px]"):
                        if report["status"] not in ("verified", "rejected"):
                            ui.button(
                                "Verify / create incident", icon="verified",
                                on_click=lambda r=report: review(r, "verify"),
                            ).props("unelevated color=positive no-caps")
                            ui.button(
                                "Reject", icon="block",
                                on_click=lambda r=report: review_with_note(r, "reject"),
                            ).props("outline color=negative no-caps")
                            if not report.get("is_anonymous", False):
                                ui.button(
                                    "Request info", icon="help",
                                    on_click=lambda r=report: review_with_note(r, "request_info"),
                                ).props("flat color=warning no-caps")
                        if report["status"] != "rejected":
                            label = "Link to another incident" if report["status"] == "verified" else "Link to existing incident"
                            ui.button(
                                label, icon="link",
                                on_click=lambda r=report: link_report_to_incident(r),
                            ).props("outline color=primary no-caps")

    def load_page(requested_page=None):
        if requested_page is not None:
            page_state["page"] = max(1, requested_page)
        params = {
            "page": page_state["page"],
            "page_size": int(page_size.value or "25"),
            "sort": sort.value or "newest",
        }
        if search.value and search.value.strip():
            params["q"] = search.value.strip()
        if status_filter.value != "all":
            params["status_filter"] = status_filter.value
        if source_filter.value != "all":
            params["source_type"] = source_filter.value
        if disaster_filter.value != "all":
            params["disaster_type"] = disaster_filter.value
        if date_from.value and date_from.value.strip():
            params["date_from"] = date_from.value.strip()
        if date_to.value and date_to.value.strip():
            params["date_to"] = date_to.value.strip()
        try:
            result = get("/api/reports/search", params=params)
            page_state["page"] = result["page"]
            first = 0 if result["total"] == 0 else (result["page"] - 1) * result["page_size"] + 1
            last = min(result["page"] * result["page_size"], result["total"])
            result_summary.set_text(f"Showing {first}–{last} of {result['total']} matching reports")
            page_summary.set_text(f"Page {result['page']} / {result['total_pages']}")
            if result["has_previous"]:
                previous_button.enable()
            else:
                previous_button.disable()
            if result["has_next"]:
                next_button.enable()
            else:
                next_button.disable()
            cards.clear()
            with cards:
                if not result["items"]:
                    ui.label("No reports match these filters.").classes("text-slate-400")
                for report in result["items"]:
                    render_report(report)
        except Exception as exc:
            _notify_error(exc)

    def apply_filters():
        load_page(1)

    def clear_filters():
        search.value = ""
        status_filter.value = "all"
        source_filter.value = "all"
        disaster_filter.value = "all"
        date_from.value = ""
        date_to.value = ""
        sort.value = "newest"
        page_size.value = "25"
        load_page(1)

    def review(report, decision):
        try:
            post(f"/api/reports/{report['id']}/review", json={"decision": decision})
            ui.notify("Report verified and incident created.", type="positive")
            ui.navigate.to("/reports")
        except Exception as exc:
            _notify_error(exc)

    load_page()


@ui.page("/submit")
def submit_page():
    user = require_user()
    if not user:
        return
    shell("Submit a report", user)
    ui.label("Describe what you observed. Do not put yourself or others at risk to collect evidence.").classes("text-sm text-slate-400")
    with ui.card().classes("bg-slate-900 border border-slate-800 w-full max-w-3xl"):
        title = ui.input("Report title", placeholder="Example: Flash flooding near ...").classes("w-full").props("outlined dark maxlength=180")
        title.props("id=cignal-report-title")
        description = ui.textarea("What happened? Include when, where and what you observed.").classes("w-full").props("outlined dark autogrow maxlength=10000")
        description.props("id=cignal-report-description")
        with ui.row().classes("w-full flex-wrap"):
            kind = ui.select(["unknown", "flood", "earthquake", "fire", "cyclone", "landslide", "other"], value="unknown", label="Disaster type").classes("flex-1 min-w-[160px]")
            urgency = ui.select(["unknown", "low", "moderate", "high", "critical"], value="unknown", label="Observed urgency").classes("flex-1 min-w-[160px]")
            language = ui.select(["en", "hi", "gu", "other"], value="en", label="Report language").classes("flex-1 min-w-[120px]")
        with ui.row().classes("w-full flex-wrap"):
            latitude = ui.number("Latitude", min=-90, max=90, format="%.6f").classes("flex-1 min-w-[160px]")
            longitude = ui.number("Longitude", min=-180, max=180, format="%.6f").classes("flex-1 min-w-[160px]")
        location_text = ui.input("Location description", placeholder="Village / district / landmark").classes("w-full").props("outlined dark")
        location_text.props("id=cignal-report-location")
        latitude.props("id=cignal-report-latitude")
        longitude.props("id=cignal-report-longitude")
        tracking = ui.label("").classes("text-orange-300 font-mono text-sm")
        upload_state = {"report_id": None}
        async def upload_file(event: events.UploadEventArguments):
            if not upload_state["report_id"]:
                ui.notify("Submit the report before attaching evidence", type="warning")
                return
            try:
                content = await event.file.read()
                result = request("POST", f"/api/reports/{upload_state['report_id']}/evidence", files={"file": (event.file.name, content, event.file.content_type)})
                ui.notify(f"Evidence attached: {result['filename']}", type="positive")
            except Exception as exc:
                _notify_error(exc)
        uploader = ui.upload(on_upload=upload_file, auto_upload=True, max_file_size=settings.max_upload_bytes).props("accept=.jpg,.jpeg,.png,.webp,.pdf").classes("w-full")
        uploader.disable()
        def submit_report():
            if not title.value or not description.value or len(description.value.strip()) < 20:
                ui.notify("Enter a title and at least 20 characters of description", type="warning")
                return
            try:
                result = post("/api/reports", json={
                    "title": title.value, "description": description.value,
                    "disaster_type": kind.value, "urgency": urgency.value,
                    "latitude": latitude.value, "longitude": longitude.value,
                    "location_text": location_text.value or None, "language": language.value,
                })
                upload_state["report_id"] = result["id"]
                tracking.set_text(f"Submitted. Tracking code: {result['tracking_code']}")
                ui.run_javascript("window.CignalDrafts?.clear()")
                uploader.enable()
                ui.notify("Report submitted; AI triage is queued for analyst review.", type="positive")
            except Exception as exc:
                _notify_error(exc)
        submit_button = ui.button("Submit report", icon="send", on_click=submit_report).props("unelevated color=primary no-caps").classes("w-full mt-2")
        submit_button.props("id=cignal-submit-button")
        ui.label("Accepted evidence: JPEG, PNG, WebP or PDF · maximum size configured by server · remove identifying metadata before sharing where appropriate.").classes("text-[10px] text-slate-500")


@ui.page("/submit-anonymously")
def anonymous_report_page():
    ui.colors(primary="#38bdf8", dark="#08111f")
    ui.dark_mode().enable()
    ui.page_title("Anonymous report · Cignal")
    ui.add_head_html(f'<meta name="cignal-api-url" content="{settings.api_public_url}"><link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#08111f"><script defer src="/register-sw.js"></script><script defer src="/offline-draft.js"></script>')
    with ui.column().classes("w-full max-w-3xl mx-auto p-5 gap-4"):
        with ui.row().classes("items-center gap-3"):
            ui.icon("radar", size="2rem").classes("text-orange-400")
            ui.label("CIGNAL · ANONYMOUS REPORTING").classes("text-lg font-black tracking-[0.16em] text-white")
        ui.label("Submit an observation without creating an account").classes("text-2xl font-bold text-white")
        ui.label("Provide only information you can safely share. Submission is not confirmation of an incident; an analyst reviews the report.").classes("text-sm text-slate-400")
        title = ui.input("Report title").classes("w-full").props("outlined dark maxlength=180")
        title.props("id=cignal-report-title")
        description = ui.textarea("What did you observe? (minimum 20 characters)").classes("w-full").props("outlined dark autogrow maxlength=10000")
        description.props("id=cignal-report-description")
        location = ui.input("Location description").classes("w-full").props("outlined dark")
        location.props("id=cignal-report-location")
        with ui.row().classes("w-full"):
            lat = ui.number("Latitude", min=-90, max=90).classes("flex-1")
            lat.props("id=cignal-report-latitude")
            lon = ui.number("Longitude", min=-180, max=180).classes("flex-1")
            lon.props("id=cignal-report-longitude")
        code_label = ui.label("").classes("text-orange-300 font-mono")
        def submit_anonymous():
            try:
                if not title.value or not description.value or len(description.value.strip()) < 20:
                    ui.notify("Enter a title and at least 20 characters of description", type="warning")
                    return
                result = post("/api/reports", json={"title": title.value, "description": description.value, "location_text": location.value or None, "latitude": lat.value, "longitude": lon.value, "language": "en", "disaster_type": "unknown", "urgency": "unknown"}, authenticated=False)
                code_label.set_text(f"Report received · Tracking code: {result['tracking_code']}")
                ui.run_javascript("window.CignalDrafts?.clear()")
                ui.notify("Your report was submitted anonymously", type="positive")
            except Exception as exc:
                _notify_error(exc)
        button = ui.button("Submit anonymously", icon="send", on_click=submit_anonymous).props("unelevated color=primary no-caps").classes("w-full")
        button.props("id=cignal-submit-button")
        ui.label("Queued offline drafts are submitted anonymously when connectivity returns. Keep this tab open until synchronization completes.").classes("text-xs text-orange-300")
        ui.button("Sign in", on_click=lambda: ui.navigate.to("/login")).props("flat color=primary no-caps")


@ui.page("/analytics")
def analytics_page():
    user = require_user()
    if not user:
        return
    shell("Analytics", user)
    try:
        data = get("/api/analytics/summary")
        with ui.row().classes("w-full gap-4 flex-wrap items-stretch"):
            with ui.card().classes("bg-slate-900 border border-slate-800 flex-1 min-w-[320px]"):
                _card_title("Incidents by disaster type")
                ui.echart({"tooltip": {"trigger": "item"}, "legend": {"textStyle": {"color": "#cbd5e1"}}, "series": [{"type": "pie", "radius": ["42%", "72%"], "itemStyle": {"borderRadius": 5, "borderColor": "#0f172a", "borderWidth": 2}, "label": {"color": "#e2e8f0"}, "data": data["incidents_by_type"]}]}).classes("w-full h-80")
            with ui.card().classes("bg-slate-900 border border-slate-800 flex-1 min-w-[320px]"):
                _card_title("Incidents by severity")
                ui.echart({"tooltip": {"trigger": "item"}, "xAxis": {"type": "category", "data": [x["name"] for x in data["incidents_by_severity"]], "axisLabel": {"color": "#94a3b8"}}, "yAxis": {"type": "value", "axisLabel": {"color": "#94a3b8"}, "splitLine": {"lineStyle": {"color": "#243244"}}}, "series": [{"type": "bar", "data": [x["value"] for x in data["incidents_by_severity"]], "itemStyle": {"borderRadius": [5, 5, 0, 0], "color": "#38bdf8"}}]}).classes("w-full h-80")
        with ui.card().classes("bg-slate-900 border border-slate-800 w-full"):
            _card_title("Report workflow distribution")
            ui.echart({"tooltip": {"trigger": "axis"}, "xAxis": {"type": "category", "data": [x["name"].replace("_", " ") for x in data["reports_by_status"]], "axisLabel": {"color": "#94a3b8"}}, "yAxis": {"type": "value", "axisLabel": {"color": "#94a3b8"}}, "series": [{"type": "bar", "data": [x["value"] for x in data["reports_by_status"]], "itemStyle": {"color": "#22c55e", "borderRadius": [5, 5, 0, 0]}}]}).classes("w-full h-72")
            ui.label(data["data_note"]).classes("text-xs text-slate-500")
    except Exception as exc:
        _page_error(str(exc))


@ui.page("/integrations")
def integrations_page():
    user = require_user()
    if not user:
        return
    if user.get("role") not in {"admin", "analyst"}:
        ui.navigate.to("/")
        return
    shell("Data sources", user)
    ui.label("Connectors are isolated. A source marked ready is configured, not necessarily reachable at this moment.").classes("text-sm text-slate-400")
    try:
        sources = get("/api/integrations/health")
        with ui.row().classes("w-full flex-wrap gap-3"):
            for source in sources:
                with ui.card().classes("bg-slate-900 border border-slate-800 flex-1 min-w-[270px]"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label(source["name"]).classes("font-bold text-slate-100")
                        status_colors = {"healthy": "positive", "running": "info", "degraded": "warning", "error": "negative", "stale": "warning", "needs_configuration": "warning", "not_tested": "secondary"}
                        status_labels = {"healthy": "HEALTHY", "running": "RUNNING", "degraded": "DEGRADED", "error": "ERROR", "stale": "STALE", "needs_configuration": "NEEDS CONFIG", "not_tested": "NOT TESTED"}
                        ui.badge(status_labels.get(source["health_status"], "UNKNOWN"), color=status_colors.get(source["health_status"], "secondary")).props("outline")
                    ui.label(source["note"]).classes("text-xs text-slate-400")
                    latest = source.get("latest_run")
                    if latest:
                        ui.label(
                            f"Last run: {latest['status']} · {latest['records_inserted']} new · "
                            f"{latest['records_duplicate']} duplicate · {latest['records_rejected']} rejected"
                        ).classes("text-xs text-slate-300")
                        if latest.get("failure_code"):
                            ui.label(f"Failure: {latest['failure_code']}").classes("text-xs text-orange-300")
                    else:
                        ui.label("No ingestion run recorded yet.").classes("text-xs text-slate-500")
                    ui.label(source["source_type"]).classes("font-mono text-[10px] text-slate-500")
        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            ui.button("Run ingestion now", icon="sync", on_click=run_ingestion).props("unelevated color=primary no-caps")
            ui.label("Scheduled ingestion runs in the background. Per-source outcomes and record counts are tracked.").classes("text-xs text-slate-500")
        with ui.card().classes("bg-slate-900 border border-slate-800 w-full"):
            _card_title("Historical dataset import", "UTF-8 CSV · up to 5 MB and 5,000 rows · title and description required")
            ui.label("Optional columns: disaster_type, urgency, latitude, longitude, location_text, language, source_url.").classes("text-xs text-slate-400")
            async def import_csv(event: events.UploadEventArguments):
                try:
                    raw = await event.file.read()
                    result = request("POST", "/api/import/historical/csv", files={"file": (event.file.name, raw, "text/csv")})
                    ui.notify(f"Imported {result['inserted']} historical reports; skipped {result['skipped']} rows", type="positive")
                except Exception as exc:
                    _notify_error(exc)
            ui.upload(on_upload=import_csv, auto_upload=True, max_file_size=5 * 1024 * 1024).props("accept=.csv").classes("w-full")
        with ui.card().classes("bg-slate-900 border border-slate-800 w-full"):
            _card_title("Weather context", "Context only; forecasts do not verify an incident")
            with ui.row().classes("w-full flex-wrap"):
                weather_lat = ui.number("Latitude", value=23.0225, min=-90, max=90).classes("flex-1 min-w-[140px]")
                weather_lon = ui.number("Longitude", value=72.5714, min=-180, max=180).classes("flex-1 min-w-[140px]")
                weather_result = ui.column().classes("w-full")
            def load_weather():
                try:
                    result = get("/api/weather/forecast", params={"latitude": weather_lat.value, "longitude": weather_lon.value})
                    current = result["data"].get("current", {})
                    with weather_result:
                        weather_result.clear()
                        ui.label(f"Temperature: {current.get('temperature_2m', '—')} °C · Rain: {current.get('precipitation', '—')} mm · Wind: {current.get('wind_speed_10m', '—')} km/h").classes("text-sm text-orange-300")
                        ui.label(f"Provider: {result['provider']} · provenance: {result['provenance']} · attribution: {result['attribution']}").classes("text-xs text-slate-500")
                except Exception as exc:
                    _notify_error(exc)
            ui.button("Load weather context", icon="cloud", on_click=load_weather).props("outline color=primary no-caps")
    except Exception as exc:
        _page_error(str(exc))


def run_ingestion():
    try:
        result = post("/api/integrations/ingest")
        ui.notify(result["message"], type="positive" if result["queued"] else "warning")
    except Exception as exc:
        _notify_error(exc)


@ui.page("/admin")
def admin_page():
    user = require_user()
    if not user:
        return
    if user.get("role") != "admin":
        ui.navigate.to("/")
        return
    shell("Administration", user)
    with ui.tabs().classes("text-orange-300") as tabs:
        users_tab = ui.tab("Users")
        audit_tab = ui.tab("Audit log")
    with ui.tab_panels(tabs, value=users_tab).classes("w-full bg-transparent"):
        with ui.tab_panel(users_tab).classes("px-0"):
            with ui.card().classes("bg-slate-900 border border-slate-800 w-full"):
                _card_title("Create account", "New users must configure MFA after first sign-in if required by policy.")
                with ui.row().classes("w-full flex-wrap"):
                    new_name = ui.input("Display name").classes("flex-1 min-w-[180px]").props("outlined dark")
                    new_email = ui.input("Email").classes("flex-1 min-w-[180px]").props("outlined dark")
                with ui.row().classes("w-full flex-wrap"):
                    new_password = ui.input("Temporary password (14+ characters)", password=True).classes("flex-1 min-w-[180px]").props("outlined dark")
                    new_role = ui.select(["admin", "analyst", "responder", "citizen"], value="analyst", label="Role").classes("w-48")
                def add_user():
                    try:
                        result = post("/api/users", json={"display_name": new_name.value, "email": new_email.value, "password": new_password.value, "role": new_role.value})
                        ui.notify(f"Created {result['email']}", type="positive")
                        ui.navigate.to("/admin")
                    except Exception as exc:
                        _notify_error(exc)
                ui.button("Create user", on_click=add_user).props("unelevated color=primary no-caps")
            try:
                rows = get("/api/users")
                with ui.card().classes("bg-slate-900 border border-slate-800 w-full mt-4"):
                    _card_title("Accounts")
                    ui.table(columns=[{"name": "display_name", "label": "Name", "field": "display_name"}, {"name": "email", "label": "Email", "field": "email"}, {"name": "role", "label": "Role", "field": "role"}, {"name": "mfa", "label": "MFA", "field": "mfa"}, {"name": "active", "label": "Active", "field": "active"}], rows=[{**r, "mfa": "Enabled" if r["mfa_enabled"] else "Not enabled", "active": "Yes" if r["is_active"] else "No"} for r in rows], row_key="id").classes("w-full")
                target_user = ui.select({row["id"]: f"{row['email']} · {row['role']}" for row in rows if row["id"] != user["id"]}, label="Select account to manage").classes("w-full max-w-xl")
                with ui.row().classes("gap-2 flex-wrap"):
                    def toggle_user(active):
                        try:
                            if not target_user.value:
                                ui.notify("Select an account first", type="warning")
                                return
                            patch(f"/api/users/{target_user.value}", json={"is_active": active})
                            ui.notify("Account updated", type="positive")
                            ui.navigate.to("/admin")
                        except Exception as exc:
                            _notify_error(exc)
                    ui.button("Activate", on_click=lambda: toggle_user(True)).props("outline color=positive no-caps")
                    ui.button("Deactivate", on_click=lambda: toggle_user(False)).props("outline color=negative no-caps")
                    def reset_selected_mfa():
                        try:
                            if not target_user.value:
                                ui.notify("Select an account first", type="warning")
                                return
                            post(f"/api/users/{target_user.value}/mfa/reset")
                            ui.notify("MFA reset; user must enroll again", type="warning")
                            ui.navigate.to("/admin")
                        except Exception as exc:
                            _notify_error(exc)
                    ui.button("Reset MFA", on_click=reset_selected_mfa).props("outline color=warning no-caps")
            except Exception as exc:
                _page_error(str(exc))
        with ui.tab_panel(audit_tab).classes("px-0"):
            try:
                logs = get("/api/audit", params={"limit": 150})
                ui.table(columns=[{"name": "created_at", "label": "Time", "field": "created_at"}, {"name": "action", "label": "Action", "field": "action"}, {"name": "resource_type", "label": "Resource", "field": "resource_type"}, {"name": "actor_user_id", "label": "Actor ID", "field": "actor_user_id"}], rows=logs, row_key="id").classes("w-full bg-slate-900")
            except Exception as exc:
                _page_error(str(exc))


@ui.page("/settings")
def settings_page():
    user = require_user()
    if not user:
        return
    shell("Settings", user)
    with ui.row().classes("w-full gap-4 flex-wrap items-stretch"):
        with ui.card().classes("bg-slate-900 border border-slate-800 flex-1 min-w-[280px]"):
            _card_title("Account security", "Authenticator MFA and recovery codes")
            ui.label(f"Signed in as {user['email']}").classes("text-sm text-slate-300")
            ui.label(f"Role: {user['role']} · MFA: {'enabled' if user.get('mfa_enabled') else 'not enabled'}").classes("text-xs text-slate-400")
            secret_output = ui.label("").classes("text-xs font-mono text-orange-300 break-all")
            mfa_code = ui.input("Authenticator code").props("outlined dark maxlength=6")
            recovery_box = ui.column().classes("w-full")
            def start_mfa():
                try:
                    result = post("/api/auth/mfa/start")
                    secret_output.set_text(f"Secret: {result['secret']} · URI: {result['provisioning_uri']}")
                except Exception as exc:
                    _notify_error(exc)
            def verify_mfa():
                try:
                    result = post("/api/auth/mfa/verify", json={"code": mfa_code.value})
                    user["mfa_enabled"] = True
                    app.storage.user["user"] = user
                    with recovery_box:
                        ui.label("Save these one-time recovery codes offline. They will not be shown again.").classes("text-orange-300 text-sm font-bold")
                        ui.code("\n".join(result["recovery_codes"]))
                    ui.notify("MFA enabled", type="positive")
                except Exception as exc:
                    _notify_error(exc)
            if not user.get("mfa_enabled"):
                ui.button("Start MFA enrollment", on_click=start_mfa).props("outline color=primary no-caps")
                ui.button("Verify code", on_click=verify_mfa).props("unelevated color=primary no-caps")
            else:
                ui.badge("MFA ACTIVE", color="positive")
        with ui.card().classes("bg-slate-900 border border-slate-800 flex-1 min-w-[280px]"):
            _card_title("Browser notifications", "Optional Web Push requires a secure HTTPS origin and VAPID configuration.")
            ui.label("Browser notifications are not available on plain HTTP remote connections. In-app notifications remain available.").classes("text-sm text-slate-400")
            ui.label("Push subscription support is exposed by the API and service worker. Enable it only after configuring VAPID keys and HTTPS.").classes("text-xs text-slate-500")
            def enable_push():
                try:
                    config = get("/api/push/public-key")
                    if not config.get("enabled"):
                        ui.notify("Configure VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY on the server first", type="warning")
                        return
                    import json
                    ui.run_javascript(
                        "window.CignalPush.enable(" + json.dumps(settings.api_public_url) + "," +
                        json.dumps(app.storage.user.get("access_token")) + "," + json.dumps(config["public_key"]) +
                        ").then(()=>alert('Browser push enabled')).catch(error=>alert(error.message));"
                    )
                except Exception as exc:
                    _notify_error(exc)
            ui.button("Enable browser push", on_click=enable_push).props("outline color=primary no-caps")
        with ui.card().classes("bg-slate-900 border border-slate-800 flex-1 min-w-[280px]"):
            _card_title("Data and privacy")
            ui.label(f"Evidence storage: {settings.storage_backend}").classes("text-sm text-slate-300")
            ui.label(f"Configured retention: {settings.retention_days} days").classes("text-sm text-slate-300")
            ui.label("Retention cleanup is an operator-controlled scheduled task; records are not silently deleted by the UI.").classes("text-xs text-slate-500")
    try:
        rows = get("/api/notifications")
        with ui.card().classes("bg-slate-900 border border-slate-800 w-full"):
            _card_title("Recent notifications")
            if not rows:
                ui.label("No notifications yet.").classes("text-slate-400")
            for row in rows[:15]:
                with ui.row().classes("w-full justify-between items-center border-b border-slate-800 py-2"):
                    with ui.column().classes("gap-0"):
                        ui.label(row["title"]).classes("text-sm font-semibold text-slate-100")
                        ui.label(row["body"]).classes("text-xs text-slate-400")
                    if not row["is_read"]:
                        ui.button("Mark read", on_click=lambda x=row: mark_read(x)).props("flat dense color=primary no-caps")
    except Exception as exc:
        _page_error(str(exc))


def mark_read(row):
    try:
        post(f"/api/notifications/{row['id']}/read")
        ui.navigate.to("/settings")
    except Exception as exc:
        _notify_error(exc)


@ui.page("/health")
def app_health_page():
    ui.navigate.to("/login")


if __name__ == "__main__":
    ui.run(host="0.0.0.0", port=8080, title="Cignal", favicon="🌐", storage_secret=settings.app_secret_key, reload=False, show=False)
