"""Durable order approval state; imported by core.models."""

from django.db import models

from core.models import UUIDTimestampedModel


class CompanyApprovalSettings(UUIDTimestampedModel):
    venue = models.OneToOneField("core.Venue", on_delete=models.CASCADE, related_name="approval_settings")
    mode = models.CharField(max_length=16, default="direct", choices=[("direct", "Direct"), ("approval", "Approval")])
    approval_type = models.CharField(
        max_length=16, null=True, blank=True, choices=[("one_of", "One of"), ("sequential", "Sequential")]
    )
    version = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey("core.User", null=True, on_delete=models.SET_NULL)


class CompanyApprovalApprover(UUIDTimestampedModel):
    settings = models.ForeignKey(CompanyApprovalSettings, on_delete=models.CASCADE, related_name="approvers")
    membership = models.ForeignKey("core.VenueUser", on_delete=models.DO_NOTHING, db_constraint=False)
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["settings", "membership"], name="approval_settings_member_unique"),
            models.UniqueConstraint(fields=["settings", "position"], name="approval_settings_position_unique"),
        ]


class OrderApprovalCycle(UUIDTimestampedModel):
    order = models.ForeignKey("core.Order", on_delete=models.CASCADE, related_name="approval_cycles")
    cycle_number = models.PositiveIntegerField()
    approval_type = models.CharField(max_length=16)
    order_revision = models.PositiveIntegerField()
    status = models.CharField(max_length=16, default="active")
    completed_at = models.DateTimeField(null=True)
    created_by = models.ForeignKey("core.User", null=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "cycle_number"], name="approval_cycle_number_unique"),
            models.UniqueConstraint(
                fields=["order"], condition=models.Q(status="active"), name="approval_cycle_one_active"
            ),
        ]


class OrderApprovalStep(UUIDTimestampedModel):
    cycle = models.ForeignKey(OrderApprovalCycle, on_delete=models.CASCADE, related_name="steps")
    membership_id = models.UUIDField()
    user_id = models.UUIDField()
    name = models.TextField()
    position = models.PositiveIntegerField()
    status = models.CharField(max_length=16, default="waiting")
    order_revision = models.PositiveIntegerField()
    activated_at = models.DateTimeField(null=True)
    decided_at = models.DateTimeField(null=True)
    comment = models.TextField(blank=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["cycle", "position"], name="approval_step_position_unique"),
            models.UniqueConstraint(fields=["cycle", "membership_id"], name="approval_step_member_unique"),
        ]


class OrderRevision(UUIDTimestampedModel):
    order = models.ForeignKey("core.Order", on_delete=models.CASCADE, related_name="revisions")
    revision = models.PositiveIntegerField()
    content = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["order", "revision"], name="order_revision_unique")]


class OrderBusinessEvent(UUIDTimestampedModel):
    order = models.ForeignKey("core.Order", on_delete=models.CASCADE, related_name="business_events")
    event = models.CharField(max_length=40)
    actor_id = models.UUIDField(null=True)
    actor_name = models.TextField(blank=True)
    revision = models.PositiveIntegerField()
    cycle_id = models.UUIDField(null=True)
    comment = models.TextField(blank=True)
    changes = models.JSONField(default=list)


class OrderApprovalCommand(UUIDTimestampedModel):
    actor_id = models.UUIDField()
    command_id = models.UUIDField()
    operation = models.CharField(max_length=32)
    target_id = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    order = models.ForeignKey("core.Order", on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["actor_id", "command_id"], name="approval_command_unique")]
