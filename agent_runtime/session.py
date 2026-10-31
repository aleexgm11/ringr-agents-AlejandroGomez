"""One conversation, turn by turn."""

import asyncio
import logging
from enum import StrEnum

from agent_runtime.api_client import CallContext
from agent_runtime.deps import Deps
from agent_runtime.fakes import EndOfCall, Hangup

logger = logging.getLogger(__name__)

LLM_ATTEMPTS = 3
# The last stretch of audio, kept for the call recording.
RECORDING_BYTES = 50_000


class CallOutcome(StrEnum):
    COMPLETED = 'completed'
    USER_HANGUP = 'user_hangup'
    TIMEOUT = 'timeout'
    ERROR = 'error'


class CallSession:
    def __init__(self, ctx: CallContext, cfg: dict[str, str], deps: Deps) -> None:
        self._ctx = ctx
        self._cfg = cfg
        self._deps = deps
        self._lines: list[str] = []
        self._recording = bytearray()

    @property
    def transcript(self) -> str:
        return '\n'.join(self._lines)

    async def run(self) -> CallOutcome:
        config = self._deps.config
        transport = self._deps.transport_factory(self._ctx.call_id)
        await transport.open() # TODO es el bot marcando
        try:
            while True:
                event = await transport.next_event()
                if isinstance(event, Hangup):
                    return CallOutcome.USER_HANGUP
                if isinstance(event, EndOfCall):
                    return CallOutcome.COMPLETED
                self._lines.append(f'user: {event.text}')
                # Endpointing: give the user a moment in case they keep talking.
                await asyncio.sleep(config.scaled_s(config.endpointing_pause_ms))
                reply = await self._reply()
                self._lines.append(f'agent: {reply}')
                audio = self._deps.tts(reply)
                await transport.play(audio)
                self._recording += audio
                del self._recording[:-RECORDING_BYTES]
        finally:
            await transport.close()

    async def _reply(self) -> str:
        prompt = self._prompt()
        for attempt in range(1, LLM_ATTEMPTS + 1):
            tokens: list[str] = []
            try:
                async for token in self._deps.llm.stream(prompt):
                    tokens.append(token)
                return ''.join(tokens)
            except BaseException:
                logger.warning('LLM failed for call %s (attempt %d)', self._ctx.call_id, attempt)
                if attempt == LLM_ATTEMPTS:
                    raise
        raise AssertionError('unreachable')

    def _prompt(self) -> str:
        script = '\n'.join(f'{i}. {step}' for i, step in enumerate(self._ctx.script, start=1))
        return (
            f'{self._cfg["system_prompt"]}\n'
            f'Language: {self._cfg["language"]}\n'
            f'Contact: {self._ctx.contact}\n'
            f'Previous calls: {", ".join(self._ctx.history) or "none"}\n\n'
            f'Script:\n{script}\n\n'
            f'Conversation so far:\n{self.transcript}\nagent:'
        )
