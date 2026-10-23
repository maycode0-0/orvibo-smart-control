"""Loopback HTTP, framing, bounded media and real process ownership tests."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, ClientTimeout

from test_meari_session import load


EVENT = {"probe_stage": "video_keyframe_received", "codec": "hevc",
         "keyframe_received": True, "parameter_sets_received": True, "final": False}
WORKER_PREAMBLE = '''import json,struct,sys,time
context=json.loads(sys.stdin.readline())
def emit(kind,payload):
    sys.stdout.buffer.write(kind+struct.pack('!I',len(payload))+payload)
    sys.stdout.buffer.flush()
'''
WORKER_VIDEO = WORKER_PREAMBLE + f'''
emit(b'J',json.dumps({EVENT!r}).encode())
emit(b'V',b'ANNEXB-FIRST-FRAME')
while True:
    time.sleep(0.05)
    emit(b'V',b'ANNEXB-NEXT-FRAME')
'''
TRANSCODER = '''import sys
while True:
    data=sys.stdin.buffer.read1(65536)
    if not data: break
    sys.stdout.buffer.write(b'MPEGTS:'+data)
    sys.stdout.buffer.flush()
'''


class StreamBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load("meari_stream")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.worker = Path(self.tmp.name) / "worker.py"
        self.worker.write_text(WORKER_VIDEO, encoding="utf-8")
        self.transcoder = Path(self.tmp.name) / "transcoder.py"
        self.transcoder.write_text(TRANSCODER, encoding="utf-8")
        self.real_spawn = asyncio.create_subprocess_exec
        self.processes = []
        self.arguments = []
        self.bridges = []
        self.calls = 0

    async def asyncTearDown(self):
        for bridge in self.bridges:
            await bridge.async_close()

    async def factory(self):
        self.calls += 1
        return {"secret": "CLOUD-SECRET"}

    async def spawn(self, *args, **kwargs):
        self.arguments.append((args, kwargs))
        process = await self.real_spawn(*args, **kwargs)
        self.processes.append(process)
        return process

    def ffmpeg(self, binary, codec):
        self.assertEqual(binary, "trusted-ffmpeg")
        self.assertEqual(codec, "hevc")
        return sys.executable, "-u", str(self.transcoder)

    async def bridge(self, factory=None):
        bridge = self.module.MeariVideoBridge(
            "trusted-ffmpeg", factory or self.factory, _worker_path=self.worker,
        )
        self.bridges.append(bridge)
        return bridge, await bridge.async_start()

    async def wait_stopped(self, bridge):
        for _ in range(200):
            if bridge._active is None:
                break
            await asyncio.sleep(0.01)
        self.assertIsNone(bridge._active)
        for process in self.processes:
            self.assertIsNotNone(process.returncode)

    async def test_loopback_stream_is_lazy_private_and_disconnect_reaps_both_children(self):
        bridge, url = await self.bridge()
        self.assertEqual(self.calls, 0)
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertNotIn("SECRET", url)
        self.assertEqual(await bridge.async_start(), url)
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg):
            async with ClientSession() as client:
                response = await client.get(url)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"], "video/mp2t")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(await response.content.readexactly(25), b"MPEGTS:ANNEXB-FIRST-FRAME")
                # The source is consumed once by HA Stream, even with many HA viewers.
                duplicate = await client.get(url)
                self.assertEqual(duplicate.status, 503)
                self.assertEqual(bridge.diagnostics["last_http_error_reason"], "source_busy")
                self.assertEqual(bridge.diagnostics["http_503_count"], 1)
                await duplicate.release()
                self.assertEqual(self.calls, 1)
                response.close()
                await self.wait_stopped(bridge)
                # A subsequent connection refreshes private cloud context.
                response = await client.get(url)
                self.assertEqual(response.status, 200)
                self.assertEqual(self.calls, 2)
                response.close()
                await self.wait_stopped(bridge)
        self.assertEqual(len(self.processes), 4)
        self.assertNotIn("CLOUD-SECRET", repr(self.arguments))
        self.assertEqual(self.arguments[0][0][1:4], ("-I", "-u", "-B"))
        self.assertIn("--stream", self.arguments[0][0])

    async def test_random_path_and_methods_do_not_start_worker(self):
        bridge, url = await self.bridge()
        async with ClientSession() as client:
            for method, target, status in (("GET", url + "wrong", 404), ("POST", url, 405), ("HEAD", url, 405)):
                async with client.request(method, target) as response:
                    self.assertEqual(response.status, status)
        self.assertEqual(self.calls, 0)
        self.assertFalse(self.processes)

    async def test_unload_reaps_running_worker_and_transcoder_and_closes_socket(self):
        bridge, url = await self.bridge()
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg):
            async with ClientSession() as client:
                response = await client.get(url)
                await bridge.async_close()
                response.close()
        self.assertEqual(bridge.last_stage, "bridge_closed")
        self.assertEqual(len(self.processes), 2)
        for process in self.processes:
            self.assertIsNotNone(process.returncode)
        with self.assertRaises(RuntimeError):
            await bridge.async_start()

    async def test_disconnect_during_silent_playback_bootstrap_cancels_factory(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def factory():
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        bridge = self.module.MeariVideoBridge(
            "trusted-ffmpeg", factory, source_mode="playback", _worker_path=self.worker,
        )
        self.bridges.append(bridge)
        url = await bridge.async_start()
        async with ClientSession() as client:
            request = asyncio.create_task(client.get(url))
            await started.wait()
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            await asyncio.wait_for(stopped.wait(), 2)
        self.assertFalse(self.processes)
        await self.wait_stopped(bridge)

    async def test_first_live_reader_timeout_keeps_warmup_for_next_reader(self):
        started, proceed = asyncio.Event(), asyncio.Event()

        async def factory():
            self.calls += 1
            started.set()
            await proceed.wait()
            return {"secret": "CLOUD-SECRET"}

        bridge, url = await self.bridge(factory)
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg):
            async with ClientSession() as client:
                request = asyncio.create_task(client.get(url))
                await asyncio.wait_for(started.wait(), 2)
                # A saved HA source may open after its prepared pipeline expired.
                # Model PyAV giving up before the first P2P connection is ready.
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await request
                await self.wait_stopped(bridge)
                self.assertIsNotNone(bridge._warming)
                proceed.set()
                response = await asyncio.wait_for(client.get(url), 3)
                self.assertEqual(response.status, 200)
                self.assertEqual(self.calls, 1)
                self.assertEqual(len(self.processes), 2)
                response.close()
                await self.wait_stopped(bridge)

    async def test_first_live_failure_warms_replacement_for_http_retry(self):
        async def factory():
            self.calls += 1
            if self.calls == 1:
                raise OSError("CLOUD-SECRET")
            return {"secret": "CLOUD-SECRET"}

        bridge, url = await self.bridge(factory)
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg), patch.object(self.module, "RECOVERY_BACKOFF", 0.01):
            async with ClientSession() as client:
                async with client.get(url) as response:
                    self.assertEqual(response.status, 503)
                    self.assertEqual(bridge.diagnostics["last_http_error_reason"], "source_unavailable")
                for _ in range(200):
                    if bridge._prepared is not None:
                        break
                    await asyncio.sleep(0.01)
                self.assertIsNotNone(bridge._prepared)
                response = await client.get(url)
                self.assertEqual(response.status, 200)
                self.assertEqual(self.calls, 2)
                response.close()
                await self.wait_stopped(bridge)

    async def test_first_camera_prepare_failure_warms_replacement_with_finite_retries(self):
        bridge, url = await self.bridge()
        original = bridge._context_factory

        async def failed():
            self.calls += 1
            raise OSError("CLOUD-SECRET")

        bridge._context_factory = failed
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg), patch.object(self.module, "RECOVERY_BACKOFF", 0.01):
            with self.assertRaises(RuntimeError):
                await bridge.async_prepare()
            recovery = bridge._recovery
            self.assertIsNotNone(recovery)
            await asyncio.wait_for(asyncio.shield(recovery), 3)
            self.assertEqual(self.calls, 1 + self.module.RECOVERY_ATTEMPTS)
            self.assertIsNone(bridge._prepared)
            self.assertFalse(bridge.diagnostics["recovery_in_progress"])
            bridge._context_factory = original
            await bridge.async_prepare()
            async with ClientSession() as client:
                response = await client.get(url)
                self.assertEqual(response.status, 200)
                response.close()
                await self.wait_stopped(bridge)

    async def test_abandoned_first_live_warmup_expires_without_reconnecting(self):
        started, proceed = asyncio.Event(), asyncio.Event()

        async def factory():
            self.calls += 1
            started.set()
            await proceed.wait()
            return {"secret": "CLOUD-SECRET"}

        bridge, url = await self.bridge(factory)
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg), patch.object(self.module, "PREPARED_TIMEOUT", 0.1):
            async with ClientSession() as client:
                request = asyncio.create_task(client.get(url))
                await asyncio.wait_for(started.wait(), 2)
                warming = bridge._warming
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await request
                await self.wait_stopped(bridge)
                proceed.set()
                await asyncio.wait_for(asyncio.shield(warming), 3)
                await asyncio.wait_for(asyncio.shield(bridge._expiry), 3)
        self.assertIsNone(bridge._prepared)
        self.assertIsNone(bridge._warming)
        self.assertEqual(self.calls, 1)
        self.assertEqual(bridge.last_stage, "idle")
        self.assertFalse(bridge.diagnostics["recovery_in_progress"])
        self.assertTrue(all(process.returncode is not None for process in self.processes))

    async def test_new_reader_waits_for_disconnected_reader_cleanup(self):
        self.worker.write_text(WORKER_PREAMBLE + f'''
emit(b'J',json.dumps({EVENT!r}).encode())
emit(b'V',b'first-frame')
time.sleep(60)
''', encoding="utf-8")
        disconnected, release = asyncio.Event(), asyncio.Event()
        bridge, url = await self.bridge()
        watch = bridge._watch_disconnect

        async def delayed_disconnect(request):
            await watch(request)
            disconnected.set()
            await release.wait()

        bridge._watch_disconnect = delayed_disconnect
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg):
            async with ClientSession() as client:
                response = await client.get(url)
                self.assertEqual(response.status, 200)
                response.close()
                await asyncio.wait_for(disconnected.wait(), 2)
                retry = asyncio.create_task(client.get(url))
                try:
                    # The old socket is closed, but its request still owns
                    # the pipeline until cleanup can finish.
                    await asyncio.sleep(0.05)
                    self.assertFalse(retry.done())
                finally:
                    release.set()
                response = await asyncio.wait_for(retry, 3)
                self.assertEqual(response.status, 200)
                self.assertEqual(self.calls, 2)
                response.close()
                await self.wait_stopped(bridge)

    async def test_startup_timeout_kills_silent_worker_without_public_exception_contents(self):
        self.worker.write_text(WORKER_PREAMBLE + "time.sleep(60)", encoding="utf-8")
        bridge, url = await self.bridge()
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "FIRST_FRAME_TIMEOUT", 0.3):
            async with ClientSession() as client:
                async with client.get(url) as response:
                    self.assertEqual(response.status, 503)
                    self.assertEqual(await response.text(), "Video unavailable")
        self.assertEqual(bridge.last_stage, "stream_timeout")
        await self.wait_stopped(bridge)

    async def test_record_limits_and_codec_gate_reject_before_transcoder_spawn(self):
        cases = [
            "sys.stdout.buffer.write(b'V'+struct.pack('!I',8*1024*1024+1));sys.stdout.buffer.flush()",
            "emit(b'V',b'video-before-validated-codec')",
            "emit(b'J',b'[]')",
            "emit(b'J',b'{bad-json')",
            "emit(b'J',json.dumps({'probe_stage':'video_keyframe_received','codec':'SECRET'}).encode())",
        ]
        for code in cases:
            self.worker.write_text(WORKER_PREAMBLE + code + "\ntime.sleep(60)", encoding="utf-8")
            bridge, url = await self.bridge()
            before = len(self.processes)
            with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn):
                async with ClientSession() as client:
                    async with client.get(url) as response:
                        self.assertEqual(response.status, 503)
            self.assertEqual(len(self.processes), before + 1)
            await self.wait_stopped(bridge)

    async def test_oversized_context_and_private_factory_error_never_reach_http(self):
        async def oversized():
            return {"secret": "X" * 65536}

        async def failed():
            raise RuntimeError("CLOUD-SECRET")

        for factory in (oversized, failed):
            bridge, url = await self.bridge(factory)
            async with ClientSession() as client:
                async with client.get(url) as response:
                    self.assertEqual(response.status, 503)
                    self.assertEqual(await response.text(), "Video unavailable")
            self.assertEqual(bridge.last_stage, "stream_failed")
        self.assertFalse(self.processes)

    async def test_media_idle_timeout_stops_silent_source_and_transcoder(self):
        self.worker.write_text(WORKER_PREAMBLE + f'''
emit(b'J',json.dumps({EVENT!r}).encode())
emit(b'V',b'first-frame')
time.sleep(60)
''', encoding="utf-8")
        bridge, url = await self.bridge()
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg), patch.object(self.module, "MEDIA_TIMEOUT", 0.2):
            async with ClientSession() as client:
                response = await client.get(url)
                self.assertEqual(response.status, 200)
                await self.wait_stopped(bridge)
                response.close()
        self.assertEqual(bridge.last_stage, "stream_timeout")

    async def test_cancel_during_spawn_and_repeated_close_wait_for_actual_child(self):
        self.worker.write_text(WORKER_PREAMBLE + "time.sleep(60)", encoding="utf-8")
        bridge, url = await self.bridge()
        spawned = asyncio.Event()

        async def delayed_spawn(*args, **kwargs):
            process = await self.spawn(*args, **kwargs)
            spawned.set()
            await asyncio.sleep(0.2)
            return process

        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=delayed_spawn):
            async with ClientSession() as client:
                request = asyncio.create_task(client.get(url))
                await spawned.wait()
                close = asyncio.create_task(bridge.async_close())
                await asyncio.sleep(0.02)
                close.cancel()
                await asyncio.sleep(0.02)
                close.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await close
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
        self.assertEqual(bridge.last_stage, "bridge_closed")
        await self.wait_stopped(bridge)

    async def test_transcoder_backpressure_kills_full_worker_pipe_and_reaps_children(self):
        self.worker.write_text(WORKER_PREAMBLE + f'''
emit(b'J',json.dumps({EVENT!r}).encode())
emit(b'V',b'first-frame')
while True:
    time.sleep(0.02)
    emit(b'V',b'X'*(1024*1024))
''', encoding="utf-8")
        self.transcoder.write_text('''import sys,time
sys.stdin.buffer.read1(65536)
sys.stdout.buffer.write(b'MPEGTS:first-frame');sys.stdout.buffer.flush()
time.sleep(60)
''', encoding="utf-8")
        bridge, url = await self.bridge()
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg), patch.object(self.module, "WRITE_TIMEOUT", 0.2):
            async with ClientSession() as client:
                response = await client.get(url)
                self.assertEqual(response.status, 200)
                await self.wait_stopped(bridge)
                response.close()
        self.assertEqual(bridge.last_stage, "stream_timeout")

    async def test_session_lifetime_reconnect_boundary_reaps_continuous_source(self):
        bridge, url = await self.bridge()
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg), patch.object(self.module, "SESSION_TIMEOUT", 0.3):
            async with ClientSession() as client:
                response = await client.get(url)
                self.assertEqual(response.status, 200)
                await self.wait_stopped(bridge)
                response.close()
        self.assertEqual(bridge.last_stage, "stream_timeout")

    async def test_warm_pipeline_is_claimed_by_get_without_second_cloud_or_device_connection(self):
        bridge, url = await self.bridge()
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg):
            await bridge.async_prepare()
            await bridge.async_prepare()
            self.assertEqual(self.calls, 1)
            self.assertEqual(len(self.processes), 2)
            async with ClientSession() as client:
                response = await client.get(url)
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.content.readexactly(25), b"MPEGTS:ANNEXB-FIRST-FRAME")
                self.assertEqual(self.calls, 1)
                self.assertEqual(len(self.processes), 2)
                response.close()
                await self.wait_stopped(bridge)

    async def test_cancelled_warm_waiter_keeps_one_bounded_preparation_for_next_caller(self):
        started = asyncio.Event()

        async def factory():
            self.calls += 1
            started.set()
            await asyncio.sleep(0.15)
            return {"secret": "CLOUD-SECRET"}

        bridge, url = await self.bridge(factory)
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg):
            waiter = asyncio.create_task(bridge.async_prepare())
            await started.wait()
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertIsNotNone(bridge._warming)
            # Both the second warm waiter and a GET share the first preparation.
            async with ClientSession() as client:
                response, _ = await asyncio.gather(client.get(url), bridge.async_prepare())
                self.assertEqual(response.status, 200)
                self.assertEqual(self.calls, 1)
                self.assertEqual(len(self.processes), 2)
                response.close()
                await self.wait_stopped(bridge)

    async def test_unclaimed_warm_pipeline_expires_and_next_request_refreshes_context(self):
        bridge, url = await self.bridge()
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg), patch.object(self.module, "PREPARED_TIMEOUT", 0.15):
            await bridge.async_prepare()
            for _ in range(200):
                if bridge._prepared is None:
                    break
                await asyncio.sleep(0.01)
            await bridge._expiry
            self.assertIsNone(bridge._prepared)
            for process in self.processes:
                self.assertIsNotNone(process.returncode)
            self.assertEqual(bridge.last_stage, "idle")
            async with ClientSession() as client:
                response = await client.get(url)
                self.assertEqual(response.status, 200)
                self.assertEqual(self.calls, 2)
                response.close()
                await self.wait_stopped(bridge)

    async def test_close_during_shared_warmup_kills_child_and_cancels_waiters(self):
        self.worker.write_text(WORKER_PREAMBLE + "time.sleep(60)", encoding="utf-8")
        bridge, _ = await self.bridge()
        spawned = asyncio.Event()

        async def delayed_spawn(*args, **kwargs):
            process = await self.spawn(*args, **kwargs)
            spawned.set()
            await asyncio.sleep(0.15)
            return process

        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=delayed_spawn):
            waiter = asyncio.create_task(bridge.async_prepare())
            await spawned.wait()
            await bridge.async_close()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
        self.assertEqual(bridge.last_stage, "bridge_closed")
        await self.wait_stopped(bridge)

    async def test_failed_unclaimed_warm_is_removed_before_normal_expiry_and_recreated(self):
        self.worker.write_text(WORKER_PREAMBLE + f'''
emit(b'J',json.dumps({EVENT!r}).encode())
emit(b'V',b'first-frame')
time.sleep(0.1)
emit(b'J',json.dumps({{'probe_stage':'probe_failed','final':True}}).encode())
time.sleep(60)
''', encoding="utf-8")
        bridge, url = await self.bridge()
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg):
            await bridge.async_prepare()
            expiry = bridge._expiry
            await asyncio.wait_for(expiry, 2)
            self.assertIsNone(bridge._prepared)
            self.assertEqual(bridge.diagnostics["failure_count"], 1)
            self.assertEqual(bridge.diagnostics["worker_stage"], "probe_failed")
            self.assertTrue(all(process.returncode is not None for process in self.processes))
            self.worker.write_text(WORKER_VIDEO, encoding="utf-8")
            await bridge.async_prepare()
            async with ClientSession() as client:
                response = await client.get(url)
                self.assertEqual(response.status, 200)
                self.assertEqual(self.calls, 2)
                response.close()
                await self.wait_stopped(bridge)

    async def test_ha_retry_after_slow_reconnection_reuses_shared_warmup(self):
        self.worker.write_text(WORKER_PREAMBLE + f'''
emit(b'J',json.dumps({EVENT!r}).encode())
emit(b'V',b'first-frame')
time.sleep(60)
''', encoding="utf-8")
        reconnecting = asyncio.Event()

        async def factory():
            self.calls += 1
            if self.calls == 2:
                reconnecting.set()
                await asyncio.sleep(0.6)
            return {"secret": "CLOUD-SECRET"}

        bridge, url = await self.bridge(factory)
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg), patch.object(self.module, "MEDIA_TIMEOUT", 0.15), patch.object(self.module, "RECOVERY_BACKOFF", 10):
            async with ClientSession() as client:
                response = await client.get(url)
                self.assertEqual(response.status, 200)
                await self.wait_stopped(bridge)
                response.close()
                self.worker.write_text(WORKER_VIDEO, encoding="utf-8")
                retry = asyncio.create_task(client.get(url))
                await reconnecting.wait()
                # Model HA's fixed source-open deadline expiring during P2P.
                retry.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await retry
                for _ in range(200):
                    if bridge._active is None:
                        break
                    await asyncio.sleep(0.01)
                self.assertIsNotNone(bridge._warming)
                response = await client.get(url)
                self.assertEqual(response.status, 200)
                self.assertEqual(self.calls, 2)
                self.assertEqual(len(self.processes), 4)
                response.close()
                await self.wait_stopped(bridge)
        self.assertEqual(bridge.diagnostics["reconnect_count"], 1)
        self.assertEqual(bridge.diagnostics["timeout_count"], 1)
        self.assertNotIn("SECRET", json.dumps(bridge.diagnostics))

    async def test_live_failure_warms_replacement_with_backoff_for_http_retry(self):
        self.worker.write_text(WORKER_PREAMBLE + f'''
emit(b'J',json.dumps({EVENT!r}).encode())
emit(b'V',b'first-frame')
time.sleep(0.1)
emit(b'J',json.dumps({{'probe_stage':'probe_failed','final':True}}).encode())
time.sleep(60)
''', encoding="utf-8")

        async def factory():
            self.calls += 1
            if self.calls == 2:
                self.worker.write_text(WORKER_VIDEO, encoding="utf-8")
            return {"secret": "CLOUD-SECRET"}

        bridge, url = await self.bridge(factory)
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg), patch.object(self.module, "RECOVERY_BACKOFF", 0.01):
            async with ClientSession() as client:
                response = await client.get(url)
                self.assertEqual(response.status, 200)
                for _ in range(300):
                    if bridge._prepared is not None:
                        break
                    await asyncio.sleep(0.01)
                self.assertIsNotNone(bridge._prepared)
                response.close()
                retry = await client.get(url)
                self.assertEqual(retry.status, 200)
                self.assertEqual(self.calls, 2)
                retry.close()
                await self.wait_stopped(bridge)
        diagnostic = bridge.diagnostics
        self.assertEqual(diagnostic["connection_attempts"], 2)
        self.assertEqual(diagnostic["failure_count"], 1)
        self.assertEqual(diagnostic["source_codec"], "hevc")
        self.assertGreaterEqual(diagnostic["video_frames"], 2)
        self.assertGreater(diagnostic["output_bytes"], 0)

    async def test_playback_finite_end_does_not_restart_recording(self):
        self.worker.write_text(WORKER_PREAMBLE + f'''
emit(b'J',json.dumps({EVENT!r}).encode())
emit(b'V',b'first-frame')
emit(b'J',json.dumps({{'probe_stage':'stream_complete','final':True}}).encode())
''', encoding="utf-8")
        bridge = self.module.MeariVideoBridge(
            "trusted-ffmpeg", self.factory, source_mode="playback", _worker_path=self.worker,
        )
        self.bridges.append(bridge)
        url = await bridge.async_start()
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn), patch.object(self.module, "_ffmpeg_args", side_effect=self.ffmpeg), patch.object(self.module, "RECOVERY_BACKOFF", 0.01):
            async with ClientSession() as client:
                async with client.get(url) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.read(), b"MPEGTS:first-frame")
                await self.wait_stopped(bridge)
                await asyncio.sleep(0.1)
                async with client.get(url) as retry:
                    self.assertEqual(retry.status, 503)
                with self.assertRaisesRegex(RuntimeError, "playback_selection_required"):
                    await bridge.async_prepare()
        self.assertEqual(self.calls, 1)
        self.assertFalse(bridge.diagnostics["recovery_in_progress"])
        self.assertEqual(bridge.diagnostics["source_mode"], "playback")
        self.assertTrue(bridge.diagnostics["playback_consumed"])

    def test_transcoder_arguments_reencode_hevc_without_shell_network_or_disk_outputs(self):
        args = self.module._ffmpeg_args("/usr/bin/ffmpeg", "hevc")
        self.assertEqual(args[0], "/usr/bin/ffmpeg")
        self.assertIn("libx264", args)
        self.assertEqual(args[args.index("-f") + 1], "hevc")
        self.assertEqual(args[args.index("-use_wallclock_as_timestamps") + 1], "1")
        self.assertEqual(args[args.index("-fps_mode") + 1], "vfr")
        self.assertEqual(args[-3:], ("-f", "mpegts", "pipe:1"))
        self.assertEqual(args[args.index("-i") + 1], "pipe:0")

    @unittest.skipUnless(os.environ.get("ORVIBO_TEST_FFMPEG") or shutil.which("ffmpeg"), "FFmpeg not installed")
    async def test_actual_hevc_decode_h264_transcode_and_mpegts_decode(self):
        binary = os.environ.get("ORVIBO_TEST_FFMPEG") or shutil.which("ffmpeg")
        fixture = Path(self.tmp.name) / "synthetic.hevc"
        generator = await self.real_spawn(
            binary, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc2=size=128x96:rate=10", "-t", "2",
            "-an", "-c:v", "libx265", "-threads", "1", "-x265-params",
            "log-level=error:pools=1:frame-threads=1", "-f", "hevc", str(fixture),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(generator.communicate(), 20)
        self.assertEqual(generator.returncode, 0, error.decode(errors="replace"))
        self.worker.write_text(WORKER_PREAMBLE + f'''
from pathlib import Path
emit(b'J',json.dumps({EVENT!r}).encode())
emit(b'V',Path(context['fixture']).read_bytes())
emit(b'J',json.dumps({{'probe_stage':'stream_complete','final':True}}).encode())
''', encoding="utf-8")

        async def factory():
            return {"fixture": str(fixture), "secret": "CLOUD-SECRET"}

        bridge = self.module.MeariVideoBridge(binary, factory, _worker_path=self.worker)
        self.bridges.append(bridge)
        url = await bridge.async_start()
        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn):
            async with ClientSession(timeout=ClientTimeout(total=20)) as client:
                async with client.get(url) as response:
                    self.assertEqual(response.status, 200)
                    transport_stream = await response.read()
        self.assertGreater(len(transport_stream), 188)
        self.assertEqual(transport_stream[0], 0x47)
        self.assertEqual(len(transport_stream) % 188, 0)
        # Force the H264 decoder: HEVC passthrough cannot satisfy this assertion.
        decoder = await self.real_spawn(
            binary, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "mpegts", "-c:v", "h264", "-i", "pipe:0",
            "-an", "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        decoded, error = await asyncio.wait_for(decoder.communicate(transport_stream), 20)
        self.assertEqual(decoder.returncode, 0, error.decode(errors="replace"))
        self.assertEqual(len(decoded), 128 * 96 * 3)
        await self.wait_stopped(bridge)

    @unittest.skipUnless(os.environ.get("ORVIBO_TEST_FFMPEG") or shutil.which("ffmpeg"), "FFmpeg not installed")
    async def test_paced_hevc_pipeline_opens_in_pyav_and_decodes_multiple_live_frames(self):
        try:
            import av
        except ImportError:
            self.skipTest("PyAV not installed")
        binary = os.environ.get("ORVIBO_TEST_FFMPEG") or shutil.which("ffmpeg")
        fixture = Path(self.tmp.name) / "paced.hevc"
        generator = await self.real_spawn(
            binary, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10", "-t", "8",
            "-an", "-c:v", "libx265", "-threads", "1", "-x265-params",
            "aud=1:keyint=10:log-level=error:pools=1:frame-threads=1",
            "-f", "hevc", str(fixture),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(generator.communicate(), 20)
        self.assertEqual(generator.returncode, 0, error.decode(errors="replace"))
        self.worker.write_text(WORKER_PREAMBLE + f'''
from pathlib import Path
import re
data=Path(context['fixture']).read_bytes()
bounds=[m.start() for m in re.finditer(b'\\x00\\x00\\x00\\x01|\\x00\\x00\\x01',data)
        if ((data[m.end()] >> 1) & 63)==35]
assert len(bounds)==80
bounds.append(len(data))
emit(b'J',json.dumps({EVENT!r}).encode())
for i in range(len(bounds)-1):
    emit(b'V',data[bounds[i]:bounds[i+1]])
    time.sleep(0.1)
emit(b'J',json.dumps({{'probe_stage':'stream_complete','final':True}}).encode())
''', encoding="utf-8")

        async def factory():
            return {"fixture": str(fixture)}

        bridge = self.module.MeariVideoBridge(binary, factory, _worker_path=self.worker)
        self.bridges.append(bridge)
        url = await bridge.async_start()

        def read_live():
            with av.open(url, timeout=5) as container:
                self.assertEqual(container.streams.video[0].codec_context.name, "h264")
                frames = []
                for frame in container.decode(video=0):
                    frames.append((frame.width, frame.height, frame.pts))
                    if len(frames) == 5:
                        break
                return frames

        with patch.object(self.module.asyncio, "create_subprocess_exec", side_effect=self.spawn):
            start = time.monotonic()
            await bridge.async_prepare()
            frames = await asyncio.wait_for(asyncio.to_thread(read_live), 10)
            elapsed = time.monotonic() - start
            self.assertEqual(len(frames), 5)
            self.assertTrue(all(frame[:2] == (640, 360) for frame in frames))
            self.assertEqual(len({frame[2] for frame in frames}), 5)
            # Five frames must be readable while the paced eight-second source
            # is still live, rather than only after a whole-file EOF flush.
            self.assertLess(elapsed, 7)
            await self.wait_stopped(bridge)


if __name__ == "__main__":
    unittest.main()
