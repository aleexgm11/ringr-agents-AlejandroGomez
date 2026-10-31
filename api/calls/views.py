"""Endpoints the call runtime talks to. Routes and payloads are a fixed contract."""

import json
import math
import uuid
from typing import Any

from django.db import transaction
from django.db.models import F
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from calls.models import Call, CallResult, Tenant

HISTORY_SIZE = 20


@require_GET
def call_context(request: HttpRequest, call_id: uuid.UUID) -> JsonResponse:
    """Everything the runtime needs to start a call: tenant, contact, script and history."""
    call = get_object_or_404(Call, pk=call_id)
    owner = call.campaign.ownerships.get()
    previous_calls = Call.objects.filter(
        contact=call.contact, status=Call.Status.FINISHED
    ).order_by('-created_at')[:HISTORY_SIZE]
    history = []
    # TODO puede ser que aqui haya un punto de mejora
    # En vez de una llamada por 
    for previous in previous_calls:  
        result = previous.results.first()
        if result is not None:
            history.append({'outcome': result.outcome, 'created_at': previous.created_at})

    return JsonResponse(
        {
            'call_id': str(call.id),
            'tenant': {'id': owner.tenant_id},
            'contact': {'name': call.contact.name, 'phone': call.contact.phone},
            'script': [
                {'order': step.order, 'text': step.text}
                for step in call.campaign.steps.order_by('order')
            ],
            'history': history,
        }
    )


@require_GET
def tenant_config(request: HttpRequest, tenant_id: int) -> JsonResponse:
    tenant = get_object_or_404(Tenant, pk=tenant_id)
    return JsonResponse({'language': tenant.language, 'system_prompt': tenant.system_prompt})


@csrf_exempt
@require_POST
def call_result(request: HttpRequest, call_id: uuid.UUID) -> JsonResponse:
    """Store the outcome of a call and bill its minutes to the tenant."""
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

        tenant_id = call.campaign.ownerships.get().tenant_id

        Tenant.objects.filter(pk=tenant_id).update(
            minutes_used=F('minutes_used') + math.ceil(payload['duration_s'] / 60)
        )

        return JsonResponse({'status': 'ok'})

def is_same_result(result: CallResult, payload: dict[str, Any]) -> bool:
    return all(getattr(result, field) == value for field, value in payload.items())

def parse_result(body: bytes) -> dict[str, Any]:
    data = json.loads(body)
    if not isinstance(data, dict) or data.get('outcome') not in CallResult.Outcome.values:
        raise ValueError('unknown outcome')
    duration_s = data.get('duration_s')
    if not isinstance(duration_s, int | float) or duration_s < 0:
        raise ValueError('duration_s must be a non-negative number')
    transcript = str(data.get('transcript', ''))
    return {'outcome': data['outcome'], 'duration_s': float(duration_s), 'transcript': transcript}
