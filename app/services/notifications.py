import logging
import smtplib
from email.message import EmailMessage
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Notification, PushSubscription, User
from app.services.events import publish_event

logger = logging.getLogger(__name__)


def notify_user(db: Session, user: User, title: str, body: str, category: str = "general", target_url: str | None = None) -> None:
    item = Notification(id=str(uuid4()), user_id=user.id, title=title, body=body, category=category, target_url=target_url)
    db.add(item)
    db.flush()
    publish_event("notification.created", {"user_id": user.id, "notification_id": item.id, "title": title})
    if settings.vapid_public_key and settings.vapid_private_key:
        subscriptions = db.query(PushSubscription).filter_by(user_id=user.id).all()
        for subscription in subscriptions:
            try:
                import json

                from pywebpush import webpush
                webpush(
                    subscription_info=subscription.subscription_json,
                    data=json.dumps({"title": title, "body": body, "url": target_url or "/"}),
                    vapid_private_key=settings.vapid_private_key,
                    vapid_claims={"sub": settings.vapid_claims_subject},
                    ttl=120,
                )
            except Exception:
                logger.exception("Browser push delivery failed")
    if settings.smtp_host and user.email:
        message = EmailMessage()
        message["Subject"] = f"Cignal: {title}"
        message["From"] = settings.smtp_from or settings.smtp_username
        message["To"] = user.email
        message.set_content(body)
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                if settings.smtp_use_tls:
                    smtp.starttls()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
        except Exception:
            logger.exception("Email notification delivery failed")
