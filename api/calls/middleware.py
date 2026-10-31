"""Network conditions the runtime meets in production, reproduced locally.

Every request pays a fixed latency. For a share of the writes the response arrives after the
client has given up: the view ran and its changes are stored, but the caller never hears back.
"""

import random
import time
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse


class SimulatedNetworkMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self._rng = random.Random(settings.SIMULATED_NETWORK['seed'])

    def __call__(self, request: HttpRequest) -> HttpResponse:
        network = settings.SIMULATED_NETWORK
        time.sleep(network['latency_ms'] / 1000)
        response = self.get_response(request)
        # TODO que explique esto mejor, no entiendo bien que hace. 
        if request.method == 'POST' and self._rng.random() < network['lost_response_rate']:
            time.sleep((network['client_timeout_ms'] + 500) / 1000)
        return response
