import threading
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import Any
from unittest import mock

import pytest
from django.db import connection
from django.http import HttpResponseBase
from django.test import Client
from django.utils import timezone

from calls.models import Call, CallResult, Campaign, CampaignOwnership, Contact, ScriptStep, Tenant

pytestmark = pytest.mark.django_db


@pytest.fixture
def call() -> Call:
    tenant = Tenant.objects.create(name='acme')
    campaign = Campaign.objects.create(name='acme collections')
    CampaignOwnership.objects.create(campaign=campaign, tenant=tenant)
    ScriptStep.objects.create(campaign=campaign, order=1, text='Greet the contact.')
    contact = Contact.objects.create(name='Lucia Garcia', phone='+34600000001')
    return Call.objects.create(campaign=campaign, contact=contact)


def test_context_has_the_expected_shape(client: Client, call: Call) -> None:
    body = client.get(f'/calls/{call.id}/context').json()

    assert body == {
        'call_id': str(call.id),
        'tenant': {'id': call.campaign.ownerships.get().tenant_id},
        'contact': {'name': 'Lucia Garcia', 'phone': '+34600000001'},
        'script': [{'order': 1, 'text': 'Greet the contact.'}],
        'history': [],
    }


def test_result_is_stored(client: Client, call: Call) -> None:
    response = client.post(
        f'/calls/{call.id}/result',
        {'outcome': 'completed', 'duration_s': 95.0, 'transcript': 'user: Hello'},
        content_type='application/json',
    )

    assert response.status_code == 200
    assert CallResult.objects.get(call=call).outcome == 'completed'

# TESTS FOR RACE CONDITIONS, problem 2

RESULT = {'outcome': 'completed', 'duration_s': 95.0, 'transcript': 'user: Hello'}


def post_result(client: Client, call: Call, **changes: object) -> HttpResponseBase:
    return client.post(
        f'/calls/{call.id}/result', {**RESULT, **changes}, content_type='application/json'
    )


def minutes_used(call: Call) -> int:
    return Tenant.objects.get(ownerships__campaign=call.campaign).minutes_used


def run_in_parallel(*actions: Callable[[], None]) -> None:
    """Start every action at the same moment, each in its own thread and connection."""
    barrier = threading.Barrier(len(actions))
    errors: list[BaseException] = []

    def run(action: Callable[[], None]) -> None:
        try:
            barrier.wait()
            action()
        except BaseException as exc:
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(action,)) for action in actions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]


def test_a_repeated_result_is_stored_and_billed_once(client: Client, call: Call) -> None:
    assert post_result(client, call).status_code == 200
    assert post_result(client, call).status_code == 200

    assert CallResult.objects.filter(call=call).count() == 1
    assert minutes_used(call) == 2


def test_a_different_result_for_a_reported_call_is_rejected(client: Client, call: Call) -> None:
    post_result(client, call)

    response = post_result(client, call, outcome='error')

    assert response.status_code == 409
    assert CallResult.objects.get(call=call).outcome == 'completed'
    assert minutes_used(call) == 2


def test_a_failure_while_billing_stores_nothing(client: Client, call: Call) -> None:
    with mock.patch.object(Tenant.objects, 'filter', side_effect=RuntimeError('db down')):
        with pytest.raises(RuntimeError):
            post_result(client, call)

    call.refresh_from_db()
    assert not CallResult.objects.filter(call=call).exists()
    assert call.status == Call.Status.PENDING


@pytest.mark.django_db(transaction=True)
def test_identical_results_at_the_same_time_are_stored_once(call: Call) -> None:
    statuses: list[int] = []

    def report() -> None:
        statuses.append(post_result(Client(), call).status_code)

    run_in_parallel(report, report)

    assert statuses == [200, 200]
    assert CallResult.objects.filter(call=call).count() == 1
    assert minutes_used(call) == 2


@pytest.mark.django_db(transaction=True)
def test_results_of_one_tenant_at_the_same_time_add_up(call: Call) -> None:
    calls = [call] + [
        Call.objects.create(campaign=call.campaign, contact=call.contact) for _ in range(7)
    ]

    def report(c: Call) -> None:
        post_result(Client(), c)

    run_in_parallel(*(partial(report, c) for c in calls))

    assert minutes_used(call) == 8 * 2

# TESTS FOR PETITION LAG, problem 1
def add_history(call: Call, outcomes: list[str]) -> None:
    """Finished calls for the same contact, the first outcome being the most recent."""
    for days_ago, outcome in enumerate(outcomes, start=1):
        previous = Call.objects.create(
            campaign=call.campaign,
            contact=call.contact,
            status=Call.Status.FINISHED,
            created_at=timezone.now() - timedelta(days=days_ago),
        )
        CallResult.objects.create(call=previous, outcome=outcome, duration_s=60)


def test_context_queries_do_not_grow_with_history(
    client: Client, call: Call, django_assert_num_queries: Any
) -> None:
    with django_assert_num_queries(4):
        client.get(f'/calls/{call.id}/context')

    add_history(call, ['completed'] * 20)

    with django_assert_num_queries(4):
        client.get(f'/calls/{call.id}/context')


def test_context_history_is_the_latest_finished_calls(client: Client, call: Call) -> None:
    add_history(call, ['user_hangup'] + ['completed'] * 24)

    history = client.get(f'/calls/{call.id}/context').json()['history']

    assert len(history) == 20
    assert history[0]['outcome'] == 'user_hangup'