import logging
import threading
from contextvars import ContextVar

from google import genai
from google.adk.models.google_llm import Gemini, _ResourceExhaustedError
from google.genai.errors import ClientError, APIError

from config.settings import settings

logger = logging.getLogger(__name__)

# Thread-isolated ContextVar to securely bind a client to the current generation attempt.
_active_client_ctx: ContextVar[genai.Client | None] = ContextVar("_active_client_ctx", default=None)


class GeminiClientPool:
    def __init__(self):
        keys = [k.strip() for k in settings.GEMINI_API_KEY.split(",") if k.strip()]
        assert len(keys) == 5, f"Expected 5 Gemini API keys, got {len(keys)}"
        
        self._clients = [genai.Client(api_key=k) for k in keys]
        self.size = len(self._clients)
        
        self._active_slot = 0
        self._state_lock = threading.Lock()

    def select_active(self) -> tuple[int, genai.Client]:
        """Atomically read the active slot and client pair."""
        with self._state_lock:
            return self._active_slot, self._clients[self._active_slot]

    def handle_failure(self, failed_slot: int) -> None:
        """Advance the active slot strictly forward without wraparound."""
        with self._state_lock:
            if self._active_slot == failed_slot:
                if self._active_slot < self.size - 1:
                    self._active_slot += 1

    def get_client_by_slot(self, slot: int) -> genai.Client:
        """Lock-free read of a specific client by slot."""
        return self._clients[slot]


# Singleton process-level pool
pool = GeminiClientPool()


class _SyncModelsAccessor:
    """Synchronous adapter for MCP tools to use the ADK Gemini client pool."""
    def generate_content(self, model, contents, config=None, **kwargs):
        failed_by_this_generation: set[int] = set()
        last_error = None

        for attempt in range(pool.size):
            slot, client = pool.select_active()

            # Ensure we don't retry a client that THIS generation already failed.
            if slot in failed_by_this_generation:
                found_candidate = False
                # Find the next eligible slot STRICTLY FORWARD from the current position
                for candidate in range(slot, pool.size):
                    if candidate not in failed_by_this_generation:
                        slot = candidate
                        client = pool.get_client_by_slot(slot)
                        found_candidate = True
                        break
                if not found_candidate:
                    # No eligible clients exist forward of the current slot
                    if last_error:
                        raise last_error
                    raise RuntimeError("All Gemini clients exhausted")

            token = _active_client_ctx.set(client)
            try:
                return client.models.generate_content(
                    model=model, contents=contents, config=config, **kwargs
                )
            except APIError as e:
                # In google-genai, 429 is an APIError with code 429
                if getattr(e, "code", None) == 429:
                    failed_by_this_generation.add(slot)
                    last_error = e
                    
                    logger.warning(
                        "Gemini 429 on slot %d, attempt %d/%d", 
                        slot, attempt + 1, pool.size
                    )
                    
                    # Report failure to the global pool
                    pool.handle_failure(slot)

                    # Strict forward failover exhaustion
                    if slot == pool.size - 1:
                        logger.error("Reached end of Gemini client pool (slot %d) for this generation", slot)
                        raise e  # Propagate final error
                else:
                    raise e
            finally:
                _active_client_ctx.reset(token)


class PooledGemini(Gemini):
    @property
    def api_client(self) -> genai.Client:
        """
        Returns the bound client for this generation attempt.
        Raises an error if accessed outside a valid generation context.
        """
        bound = _active_client_ctx.get()
        if bound is None:
            raise RuntimeError("Gemini.api_client accessed outside of generation context binding")
        return bound

    @property
    def models(self):
        """Returns the synchronous models accessor for MCP tools."""
        return _SyncModelsAccessor()

    async def generate_content_async(self, llm_request, stream: bool = False):
        failed_by_this_generation: set[int] = set()
        last_error = None

        for attempt in range(pool.size):
            slot, client = pool.select_active()

            # Ensure we don't retry a client that THIS generation already failed.
            if slot in failed_by_this_generation:
                found_candidate = False
                # Find the next eligible slot STRICTLY FORWARD from the current position
                for candidate in range(slot, pool.size):
                    if candidate not in failed_by_this_generation:
                        slot = candidate
                        client = pool.get_client_by_slot(slot)
                        found_candidate = True
                        break
                if not found_candidate:
                    # No eligible clients exist forward of the current slot
                    if last_error:
                        raise last_error
                    raise RuntimeError("All Gemini clients exhausted")

            token = _active_client_ctx.set(client)
            try:
                # Delegate to the ADK generate_content_async logic
                async for resp in super().generate_content_async(llm_request, stream):
                    yield resp
                
                # Successful generation
                return

            except _ResourceExhaustedError as e:
                failed_by_this_generation.add(slot)
                last_error = e
                
                logger.warning(
                    "Gemini 429 on slot %d, attempt %d/%d", 
                    slot, attempt + 1, pool.size
                )
                
                # Report failure to the global pool
                pool.handle_failure(slot)

                # Strict forward failover exhaustion
                if slot == pool.size - 1:
                    logger.error("Reached end of Gemini client pool (slot %d) for this generation", slot)
                    raise e  # Propagate final error

            except APIError as e:
                # Preserve existing error propagation for non-429 (400, 401, 403, 404, etc.)
                raise e

            finally:
                _active_client_ctx.reset(token)
