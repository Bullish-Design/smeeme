"""Queue system for workflow processing."""

from __future__ import annotations

import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue, Empty
from typing import Any, Callable, Dict, Optional

from .exceptions import SmeeQueueError, SmeeQueueConnectionError, SmeeWorkflowError
from .models import SmeeEvent, WorkflowJob, WorkflowPriority, SmeeMetrics, QueueBackend

logger = logging.getLogger(__name__)


class QueueBackendInterface(ABC):
    """Abstract interface for queue backends."""

    @abstractmethod
    def enqueue(self, job: WorkflowJob) -> None:
        """Add job to queue."""
        pass

    @abstractmethod
    def dequeue(self, timeout: float = 1.0) -> Optional[WorkflowJob]:
        """Get job from queue with timeout."""
        pass

    @abstractmethod
    def size(self) -> int:
        """Get current queue size."""
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """Check if backend is healthy."""
        pass


class MemoryQueueBackend(QueueBackendInterface):
    """In-memory queue backend."""

    def __init__(self):
        # Priority queues: higher priority numbers processed first
        self._queues: Dict[int, Queue] = {
            priority.value: Queue() for priority in WorkflowPriority
        }
        self._total_size = 0

    def enqueue(self, job: WorkflowJob) -> None:
        """Add job to appropriate priority queue."""
        priority_queue = self._queues[job.priority.value]
        priority_queue.put(job)
        self._total_size += 1
        logger.debug(f"Enqueued job {job.job_id} with priority {job.priority.name}")

    def dequeue(self, timeout: float = 1.0) -> Optional[WorkflowJob]:
        """Dequeue job from highest priority queue."""
        # Check queues from highest to lowest priority
        for priority_value in sorted(self._queues.keys(), reverse=True):
            queue = self._queues[priority_value]
            try:
                job = queue.get_nowait()
                self._total_size = max(0, self._total_size - 1)
                return job
            except Empty:
                continue

        # If no jobs available, wait on normal priority queue
        try:
            normal_queue = self._queues[WorkflowPriority.NORMAL.value]
            job = normal_queue.get(timeout=timeout)
            self._total_size = max(0, self._total_size - 1)
            return job
        except Empty:
            return None

    def size(self) -> int:
        """Get total queue size."""
        return self._total_size

    def health_check(self) -> bool:
        """Memory backend is always healthy."""
        return True


class RedisQueueBackend(QueueBackendInterface):
    """Redis-based queue backend."""

    def __init__(self, redis_url: str = "redis://localhost:6379", queue_name: str = "smeeme_jobs"):
        try:
            import redis
            self._redis = redis.from_url(redis_url, decode_responses=True)
            self._queue_name = queue_name
            # Test connection
            self._redis.ping()
        except ImportError as e:
            raise SmeeQueueConnectionError("Redis package not installed. Install with: uv add redis") from e
        except Exception as e:
            raise SmeeQueueConnectionError(f"Failed to connect to Redis at {redis_url}: {e}") from e

    def enqueue(self, job: WorkflowJob) -> None:
        """Add job to Redis queue with priority scoring."""
        try:
            job_data = job.model_dump_json()
            # Use priority as score (higher = higher priority)
            score = job.priority.value * 1000 + time.time()
            self._redis.zadd(self._queue_name, {job_data: score})
            logger.debug(f"Enqueued job {job.job_id} to Redis with score {score}")
        except Exception as e:
            raise SmeeQueueError(f"Failed to enqueue job: {e}") from e

    def dequeue(self, timeout: float = 1.0) -> Optional[WorkflowJob]:
        """Dequeue highest priority job from Redis."""
        try:
            # Get highest score (priority) job
            result = self._redis.bzpopmax(self._queue_name, timeout=int(timeout))
            if result:
                _, job_data, _ = result
                job_dict = json.loads(job_data)
                return WorkflowJob(**job_dict)
            return None
        except Exception as e:
            logger.error(f"Failed to dequeue job: {e}")
            return None

    def size(self) -> int:
        """Get queue size from Redis."""
        try:
            return self._redis.zcard(self._queue_name)
        except Exception:
            return 0

    def health_check(self) -> bool:
        """Check Redis connection health."""
        try:
            self._redis.ping()
            return True
        except Exception:
            return False


class WorkflowProcessor:
    """Processes workflow jobs using registered handlers."""

    def __init__(self):
        self._handlers: Dict[str, Callable[[WorkflowJob], Any]] = {}
        self._metrics = SmeeMetrics()

    def register_handler(self, workflow_type: str, handler: Callable[[WorkflowJob], Any]) -> None:
        """Register a workflow handler."""
        self._handlers[workflow_type] = handler
        logger.info(f"Registered handler for workflow type: {workflow_type}")

    def unregister_handler(self, workflow_type: str) -> bool:
        """Unregister a workflow handler."""
        if workflow_type in self._handlers:
            del self._handlers[workflow_type]
            logger.info(f"Unregistered handler for workflow type: {workflow_type}")
            return True
        return False

    def process_job(self, job: WorkflowJob) -> Dict[str, Any]:
        """Process a single job."""
        start_time = time.time()
        
        try:
            handler = self._handlers.get(job.workflow_type)
            if not handler:
                raise SmeeWorkflowError(f"No handler registered for workflow type: {job.workflow_type}")

            logger.info(f"Processing job {job.job_id} (type: {job.workflow_type})")

            # Execute handler
            result = handler(job)
            
            # Update metrics
            processing_time = (time.time() - start_time) * 1000  # ms
            self._metrics.queue_jobs_processed += 1
            self._update_processing_time(processing_time)

            logger.info(f"Job {job.job_id} completed in {processing_time:.1f}ms")
            
            return {
                "status": "success",
                "job_id": job.job_id,
                "processing_time_ms": processing_time,
                "result": result,
            }

        except Exception as e:
            processing_time = (time.time() - start_time) * 1000
            self._metrics.queue_jobs_failed += 1
            self._update_processing_time(processing_time)

            error_msg = f"Job {job.job_id} failed: {e}"
            logger.error(error_msg)
            
            raise SmeeWorkflowError(error_msg, job_id=job.job_id, retry_count=job.retry_count) from e

    def _update_processing_time(self, processing_time_ms: float) -> None:
        """Update average processing time metric."""
        total_jobs = self._metrics.queue_jobs_processed + self._metrics.queue_jobs_failed
        if total_jobs == 1:
            self._metrics.average_processing_time_ms = processing_time_ms
        else:
            # Running average
            current_avg = self._metrics.average_processing_time_ms
            self._metrics.average_processing_time_ms = (
                (current_avg * (total_jobs - 1) + processing_time_ms) / total_jobs
            )

    def get_metrics(self) -> SmeeMetrics:
        """Get current processing metrics."""
        return self._metrics.model_copy()


class SmeeQueue:
    """Main queue manager for workflow processing."""

    def __init__(self, backend: QueueBackend = QueueBackend.MEMORY, **kwargs):
        self.backend = self._create_backend(backend, **kwargs)
        self.processor = WorkflowProcessor()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._running = False
        self._workers = 3

    def _create_backend(self, backend: QueueBackend, **kwargs) -> QueueBackendInterface:
        """Create appropriate queue backend."""
        if backend == QueueBackend.MEMORY:
            return MemoryQueueBackend()
        elif backend == QueueBackend.REDIS:
            redis_url = kwargs.get("redis_url", "redis://localhost:6379")
            return RedisQueueBackend(redis_url)
        else:
            raise SmeeQueueError(f"Unsupported backend: {backend}")

    def enqueue_event(self, event: SmeeEvent, workflow_type: Optional[str] = None) -> WorkflowJob:
        """Enqueue event for workflow processing."""
        # Auto-classify if not specified
        if not workflow_type:
            workflow_type = event.classify_event()

        # Create job
        job = WorkflowJob(
            job_id=f"{workflow_type}_{uuid.uuid4().hex[:8]}_{int(time.time())}",
            event=event,
            workflow_type=workflow_type,
        )

        # Enqueue
        self.backend.enqueue(job)
        return job

    def register_workflow(self, workflow_type: str, handler: Callable[[WorkflowJob], Any]) -> None:
        """Register workflow handler."""
        self.processor.register_handler(workflow_type, handler)

    def start_workers(self, num_workers: int = 3) -> None:
        """Start worker threads."""
        if self._running:
            logger.warning("Workers already running")
            return

        self._workers = num_workers
        self._executor = ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix="queue-worker")
        self._running = True

        # Submit worker tasks
        for i in range(num_workers):
            self._executor.submit(self._worker_loop, f"worker-{i}")

        logger.info(f"Started {num_workers} queue workers")

    def stop_workers(self, timeout: float = 30.0) -> None:
        """Stop worker threads."""
        if not self._running:
            return

        self._running = False
        logger.info("Stopping queue workers...")

        if self._executor:
            self._executor.shutdown(wait=True, timeout=timeout)
            self._executor = None

        logger.info("Queue workers stopped")

    def _worker_loop(self, worker_id: str) -> None:
        """Main worker loop."""
        logger.debug(f"Worker {worker_id} started")

        while self._running:
            try:
                job = self.backend.dequeue(timeout=1.0)
                if job:
                    try:
                        self.processor.process_job(job)
                    except SmeeWorkflowError as e:
                        # Handle retry logic
                        if job.can_retry:
                            job.retry_count += 1
                            logger.info(f"Retrying job {job.job_id} (attempt {job.retry_count})")
                            self.backend.enqueue(job)
                        else:
                            logger.error(f"Job {job.job_id} failed permanently: {e}")

            except Exception as e:
                logger.error(f"Worker {worker_id} encountered error: {e}")

        logger.debug(f"Worker {worker_id} stopped")

    def get_status(self) -> Dict[str, Any]:
        """Get queue status."""
        return {
            "running": self._running,
            "workers": self._workers,
            "queue_size": self.backend.size(),
            "backend_healthy": self.backend.health_check(),
            "metrics": self.processor.get_metrics(),
        }

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop_workers()
