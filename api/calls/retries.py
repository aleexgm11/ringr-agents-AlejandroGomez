"""Retries of unanswered calls, following each tenant's contract."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from calls.models import Call, Tenant

CALLING_TZ = ZoneInfo('Europe/Madrid')

def schedule_retry(call: Call, tenant: Tenant, now: datetime) -> Call | None:
    """Create the next attempt of an unanswered call, if the contract allows another one."""
    if call.attempt >= tenant.max_attempts:
        return None
    return Call.objects.create(
        campaign_id=call.campaign_id,
        contact_id=call.contact_id,
        attempt=call.attempt + 1,
        retry_of=call,
        scheduled_at=next_call_time(
            now + tenant.retry_delay, tenant.calling_opens, tenant.calling_closes
        ),
    )

def next_call_time(moment: datetime, opens: time | None, closes: time | None) -> datetime:
    """The first moment, from `moment` on, within calling hours `[opens, closes)`"""
    if opens is None or closes is None:
        return moment
    local = moment.astimezone(CALLING_TZ)
    if local.time() < opens:
        return datetime.combine(local.date(), opens, tzinfo=CALLING_TZ)
    if local.time() >= closes: # Next call tomorrow opening time
        return datetime.combine(local.date() + timedelta(days=1), opens, tzinfo=CALLING_TZ)
    return moment