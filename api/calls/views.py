"""Endpoints the call runtime talks to. Routes and payloads are a fixed contract."""

import json
import math
import uuid
from typing import Any

from django.db import transaction
from django.db.models import F, OuterRef, Subquery
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from calls.models import Call, CallResult, CampaignOwnership, ScriptStep, Tenant
from calls.retries import schedule_retry

HISTORY_SIZE = 20

@require_GET
def call_context(request: HttpRequest, call_id: uuid.UUID) -> JsonResponse:
    """Everything the runtime needs to start a call: tenant, contact, script and history.

    Prefetches the last HISTORY_SIZE results to evict more calls to the database.
    """
    call = get_object_or_404(Call.objects.select_related('contact'), pk=call_id)
    owner = CampaignOwnership.objects.get(campaign_id=call.campaign_id)
    first_outcome = (
        CallResult.objects.filter(call=OuterRef('pk')).order_by('pk').values('outcome')[:1]
    )

    # TODO puede ser que aqui haya un punto de mejora
    # En vez de una peticion por cada llamada, se podria hacer una sola peticion.
    previous_calls = (
        Call.objects.filter(contact_id=call.contact_id, status=Call.Status.FINISHED)
        .annotate(outcome=Subquery(first_outcome))
        .order_by('-created_at')
        .values('outcome', 'created_at')[:HISTORY_SIZE]
    )

    return JsonResponse(
        {
            'call_id': str(call.id),
            'tenant': {'id': owner.tenant_id},
            'contact': {'name': call.contact.name, 'phone': call.contact.phone},
            'script': list(
                ScriptStep.objects.filter(campaign_id=call.campaign_id)
                .order_by('order')
                .values('order', 'text')
            ),
            'history': [p for p in previous_calls if p['outcome'] is not None],
        }
    )


@require_GET
def tenant_config(request: HttpRequest, tenant_id: int) -> JsonResponse:
    tenant = get_object_or_404(Tenant, pk=tenant_id)
    return JsonResponse({'language': tenant.language, 'system_prompt': tenant.system_prompt})


@csrf_exempt
@require_POST
def call_result(request: HttpRequest, call_id: uuid.UUID) -> JsonResponse:
    """Store the outcome of a call, bill its minutes and schedule a retry if nobody answered.

    The runtime retries when it gets no answer, so an identical repeated request answers 200
    without doing anything again. A different result for a call that already has one is a 409.
    """
    try:
        payload = parse_result(request.body)
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    # Prevent race conditions when multiple results arrive for the same call.
    with transaction.atomic(): 
        call = get_object_or_404(Call.objects.select_for_update(), pk=call_id)

        existing = call.results.first()
        if existing is not None:
            if is_same_result(existing, payload):
                return JsonResponse({'status': 'ok'})
            else:
                return JsonResponse({'error': 'call already has a different result'}, status=409)

        CallResult.objects.create(call=call, **payload)
        call.status = Call.Status.FINISHED
        call.save(update_fields=['status'])

        tenant = Tenant.objects.get(ownerships__campaign_id=call.campaign_id)
        # Added by the database, so results of the same tenant never overwrite each other.
        Tenant.objects.filter(pk=tenant.pk).update(
            minutes_used=F('minutes_used') + billed_minutes(payload)
        )
        if payload['outcome'] == CallResult.Outcome.NO_ANSWER:
            schedule_retry(call, tenant, timezone.now())

        return JsonResponse({'status': 'ok'})

def is_same_result(result: CallResult, payload: dict[str, Any]) -> bool:
    return all(getattr(result, field) == value for field, value in payload.items())

def billed_minutes(payload: dict[str, Any]) -> int:
    """Calls nobody answered are not billed."""
    if payload['outcome'] == CallResult.Outcome.NO_ANSWER:
        return 0
    return math.ceil(payload['duration_s'] / 60)

def parse_result(body: bytes) -> dict[str, Any]:
    data = json.loads(body)
    if not isinstance(data, dict) or data.get('outcome') not in CallResult.Outcome.values:
        raise ValueError('unknown outcome')
    duration_s = data.get('duration_s')
    if not isinstance(duration_s, int | float) or duration_s < 0:
        raise ValueError('duration_s must be a non-negative number')
    transcript = str(data.get('transcript', ''))
    return {'outcome': data['outcome'], 'duration_s': float(duration_s), 'transcript': transcript}
