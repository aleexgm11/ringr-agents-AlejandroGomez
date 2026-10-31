import uuid

from django.db import models
from django.utils import timezone


class Tenant(models.Model):
    name = models.CharField(max_length=100, unique=True)
    language = models.CharField(max_length=10, default='es-ES')
    system_prompt = models.TextField(default='')
    # Billed minutes, summed from every reported call result.
    minutes_used = models.PositiveIntegerField(default=0)


class Campaign(models.Model):
    name = models.CharField(max_length=200)


class CampaignOwnership(models.Model):
    """The tenant a campaign belongs to, and whose minutes its calls are billed to."""

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='ownerships')
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='ownerships')


class ScriptStep(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='steps')
    order = models.PositiveIntegerField()
    text = models.TextField()


class Contact(models.Model):
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=32)


class Call(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending'
        FINISHED = 'finished'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='calls')
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='calls')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(default=timezone.now)
    # The dialer (another service) starts pending calls once this moment has passed.
    scheduled_at = models.DateTimeField(default=timezone.now)


class CallResult(models.Model):
    class Outcome(models.TextChoices):
        COMPLETED = 'completed'
        USER_HANGUP = 'user_hangup'
        TIMEOUT = 'timeout'
        ERROR = 'error'

    call = models.ForeignKey(Call, on_delete=models.CASCADE, related_name='results')
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    # Real call duration in seconds, as measured by the runtime.
    duration_s = models.FloatField()
    transcript = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['call'], name='one_result_per_call'),
        ]
