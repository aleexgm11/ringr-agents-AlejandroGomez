import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone


class Tenant(models.Model):
    name = models.CharField(max_length=100, unique=True)
    language = models.CharField(max_length=10, default='es-ES')
    system_prompt = models.TextField(default='')
    # Billed minutes, summed from every reported call result.
    minutes_used = models.PositiveIntegerField(default=0)

    max_attempts = models.PositiveSmallIntegerField(default=1)
    retry_delay = models.DurationField(default=timedelta(0))

    calling_opens = models.TimeField(null=True, blank=True)
    calling_closes = models.TimeField(null=True, blank=True)

    class Meta:
        # The calling hours must be either both null or both set, 
        # and if set, opens must be before closes.
        constraints = [
            models.CheckConstraint(
                check=models.Q(calling_opens__isnull=True, calling_closes__isnull=True)
                | models.Q(
                    calling_opens__isnull=False,
                    calling_closes__isnull=False,
                    calling_opens__lt=models.F('calling_closes'),
                ),
                name='calling_hours_together_and_ordered',
            ),
        ]


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
    # 1 for the first call to a contact, 2 for its first retry, and so on.
    attempt = models.PositiveSmallIntegerField(default=1)
    # The unanswered call this one retries. One-to-one, so a call is never retried twice.
    retry_of = models.OneToOneField(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='retry'
    )


class CallResult(models.Model):
    class Outcome(models.TextChoices):
        COMPLETED = 'completed'
        USER_HANGUP = 'user_hangup'
        TIMEOUT = 'timeout'
        ERROR = 'error'
        NO_ANSWER = 'no_answer'

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
