from core.models import AuditActorType, AuditLog, AuditLogAction, AuditLogEntityType, User, Venue
from suppliers.models import Supplier

__all__ = ["record_audit_log", "AuditLogAction", "AuditLogEntityType"]


def record_audit_log(*, actor, action, entity_type, entity_id, summary, scope_id=None):
    """Single write path for AuditLog. `actor` is whatever request.user was
    at the call site — a core.User (JWT), a Supplier (supplier cabinet
    JWT), a Venue (venue cabinet JWT — delete_venue_cabinet authenticates
    the cabinet itself as request.user, not a core.User), or anything else
    (None, AnonymousUser, ...) for a service-token call or an unauthenticated
    context — all of which log as AuditActorType.SERVICE."""
    if isinstance(actor, User):
        actor_type = AuditActorType.CORE_USER
        actor_label = f"{actor.full_name} <{actor.email}>"
    elif isinstance(actor, Supplier):
        actor_type = AuditActorType.SUPPLIER
        actor_label = actor.display_name
    elif isinstance(actor, Venue):
        actor_type = AuditActorType.VENUE
        actor_label = actor.display_name
    else:
        actor_type = AuditActorType.SERVICE
        actor_label = "Сервисный токен"

    return AuditLog.objects.create(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        summary=summary,
        scope_id=scope_id,
        actor_type=actor_type,
        actor_id=getattr(actor, "id", None) if actor_type != AuditActorType.SERVICE else None,
        actor_label=actor_label,
    )
