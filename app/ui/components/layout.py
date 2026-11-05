from nicegui import app, ui

NAV_ITEMS = [
    ("dashboard", "Dashboard", "/", ["admin", "analyst", "responder", "citizen"]),
    ("public", "Incident monitor", "/incidents", ["admin", "analyst", "responder"]),
    ("fact_check", "Report review", "/reports", ["admin", "analyst", "citizen"]),
    ("add_location_alt", "Submit report", "/submit", ["admin", "analyst", "citizen"]),
    ("query_stats", "Analytics", "/analytics", ["admin", "analyst"]),
    ("hub", "Data sources", "/integrations", ["admin", "analyst"]),
    ("admin_panel_settings", "Administration", "/admin", ["admin"]),
    ("settings", "Settings", "/settings", ["admin", "analyst", "responder", "citizen"]),
]


def require_user():
    if not app.storage.user.get("access_token"):
        ui.navigate.to("/login")
        return None
    return app.storage.user.get("user")


def shell(title: str, user: dict):
    ui.colors(
        primary="#E58B37", secondary="#A1A1AA", accent="#E58B37", dark="#101214",
        positive="#E58B37", negative="#A1A1AA", warning="#E58B37", info="#A1A1AA",
    )
    ui.dark_mode().enable()
    ui.page_title(f"{title} · Cignal")
    ui.add_head_html('<link rel="manifest" href="/static/manifest.webmanifest"><meta name="theme-color" content="#08111f"><script defer src="/static/register-sw.js"></script><script defer src="/static/push-client.js"></script>')
    with ui.header().classes("items-center justify-between bg-slate-950 text-slate-100 border-b border-slate-800 px-5"):
        with ui.row().classes("items-center gap-3"):
            ui.icon("radar", size="2rem").classes("text-orange-400")
            with ui.column().classes("gap-0"):
                ui.label("CIGNAL").classes("text-lg font-black tracking-[0.24em]")
                ui.label("DISASTER INTELLIGENCE").classes("text-[9px] text-slate-400 tracking-[0.22em]")
        with ui.row().classes("items-center gap-3"):
            ui.label(user.get("display_name", "User")).classes("text-sm text-slate-300")
            ui.badge(user.get("role", "user").upper(), color="info")
            ui.button(icon="logout", on_click=logout).props("flat round dense color=white")
    with ui.left_drawer(value=True).classes("bg-slate-950 text-slate-200 border-r border-slate-800 w-64"):
        ui.label("WORKSPACE").classes("text-[10px] font-bold tracking-[0.2em] text-slate-500 px-3 pt-4 pb-2")
        for icon_name, label, path, roles in NAV_ITEMS:
            if user.get("role") in roles:
                active = (path == "/" and title == "Dashboard") or path.strip("/").lower() in title.lower()
                button = ui.button(label, icon=icon_name, on_click=lambda p=path: ui.navigate.to(p)).props("flat align=left no-caps")
                button.classes("w-full justify-start rounded-lg px-3 " + ("bg-slate-800 text-orange-300" if active else "text-slate-300 hover:bg-slate-900"))
        ui.separator().classes("bg-slate-800 my-4")
        ui.label("HUMAN-IN-THE-LOOP INTELLIGENCE").classes("text-[9px] tracking-[0.12em] text-slate-500 px-3")
        ui.label("AI triage assists. People verify.").classes("text-xs text-slate-400 px-3 pt-2")
    with ui.column().classes("w-full max-w-[1500px] mx-auto p-4 md:p-7 gap-5"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(title).classes("text-2xl md:text-3xl font-bold text-slate-100")
            ui.label("DEMO DATA IS MARKED · VERIFY BEFORE ACTION").classes("text-[9px] tracking-[0.12em] text-slate-500")


def logout():
    try:
        post = __import__("app.ui.api_client", fromlist=["post"]).post
        post("/api/auth/logout", json={"refresh_token": app.storage.user.get("refresh_token") or ""})
    except Exception:
        pass
    app.storage.user.clear()
    ui.navigate.to("/login")
