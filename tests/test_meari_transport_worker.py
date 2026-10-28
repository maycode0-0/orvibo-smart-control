"""Offline protocol, process-output, deadline and keyframe-gate regression tests."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
WORKER_PATH = REPO / "custom_components/orvibo_smart_control/meari_transport/worker.py"
if not WORKER_PATH.is_file():
    WORKER_PATH = Path(__file__).resolve().parent / "meari_transport/worker.py"
spec = importlib.util.spec_from_file_location("orvibo_transport_worker_test", WORKER_PATH)
WORKER = importlib.util.module_from_spec(spec)
spec.loader.exec_module(WORKER)


def context():
    return {
        "account": {"user_id": "PRIVATE-USER", "country_code": "CN", "brand": "PRIVATE-BRAND", "api_server": "https://api.example.invalid", "openapi_server": "https://openapi-cn.example.invalid", "platform_domain": None, "app_version": "5.2.6.307a13"},
        "device": {"snNum": "123456789", "hostKey": "PRIVATE-HOSTKEY-SECRET-0123456789", "iotType": 3, "devTypeID": 11, "nvrChannelId": 7, "deviceUUID": "WRONG-UUID"},
        "channel_id": 7, "timeout_seconds": 35,
        "vvp_auth": {"full_password": False, "include_licence": True},
    }


def frame(payload, *, iframe=True):
    header = bytearray(48 if iframe else 40)
    header[:4] = b"\x00\x00\x01" + bytes([0xFC if iframe else 0xFD])
    return bytes(header) + payload


H264 = b"\x00\x00\x00\x01\x67\x64\x00\x1f\xaa\x00\x00\x00\x01\x68\xee\x3c\x80\x00\x00\x00\x01\x65\x88\x84\x21"
HEVC = b"".join(b"\x00\x00\x00\x01" + bytes([kind << 1, 1]) + b"\x80" * (size - 2) for kind, size in ((32,6),(33,15),(34,4),(19,4)))


class TimeoutSocket:
    def __init__(self):
        self.timeout = 3.0

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value


class ContextAndOutputTests(unittest.TestCase):
    def test_valid_context_and_explicit_auth(self):
        self.assertEqual(WORKER.validate_context(context())["channel_id"], 7)
        for key in ("full_password", "include_licence"):
            value = context()
            del value["vvp_auth"][key]
            with self.assertRaises(ValueError):
                WORKER.validate_context(value)

    def test_untrusted_or_unsupported_context_is_rejected(self):
        for path, value in (("channel_id", True), ("timeout_seconds", float("nan")), ("timeout_seconds", 90), ("camera_tags", 1), ("vvp_auth", {})):
            raw = context()
            raw[path] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                WORKER.validate_context(raw)
        for key, value in (("iotType", 2), ("snNum", "abcd"), ("hostKey", "secret\nprivate")):
            raw = context()
            raw["device"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                WORKER.validate_context(raw)

    def test_storage_card_context_accepts_only_supported_ipc_channel_zero(self):
        for operation, date in (("month", "202610"), ("day", "20261015"),
                                ("playback", "20261015090000")):
            raw = context()
            raw["device"].update(devTypeID=2, nvrChannelId=0)
            raw["channel_id"] = 0
            raw["hdd"] = {"operation": operation, "date": date}
            with self.subTest(operation=operation):
                self.assertEqual(WORKER.validate_context(raw)["hdd"], raw["hdd"])
            for device_type, channel in ((2, 1), (17, 0), (21, 0)):
                raw["device"].update(devTypeID=device_type, nvrChannelId=channel)
                raw["channel_id"] = channel
                with self.subTest(operation=operation, device_type=device_type, channel=channel):
                    with self.assertRaises(ValueError):
                        WORKER.validate_context(raw)

    def test_quality_is_explicit_and_cannot_change_recording_requests(self):
        for quality in ("default", "fluent"):
            raw = {**context(), "stream_quality": quality}
            self.assertEqual(WORKER.validate_context(raw)["stream_quality"], quality)
        for quality in (None, True, 100, "HD", {}, []):
            with self.subTest(quality=quality), self.assertRaises(ValueError):
                WORKER.validate_context({**context(), "stream_quality": quality})
        raw = {**context(), "stream_quality": "fluent",
               "hdd": {"operation": "playback", "date": "20261015090000"}}
        with self.assertRaises(ValueError):
            WORKER.validate_context(raw)

    def test_isolated_script_rejects_input_without_leaking_secrets(self):
        result = subprocess.run([sys.executable, "-I", "-B", "-u", str(WORKER_PATH)], input=b'{"SECRET":"PRIVATE-HOSTKEY"}', capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row["probe_stage"] for row in lines], ["worker_started", "invalid_context"])
        self.assertTrue(lines[-1]["final"])
        self.assertNotIn(b"PRIVATE", result.stdout)

    def test_oversized_input_is_rejected_with_bounded_stdout(self):
        result = subprocess.run([sys.executable, "-I", "-B", "-u", str(WORKER_PATH)], input=b" " * (WORKER.MAX_INPUT_BYTES + 1), capture_output=True, timeout=5)
        self.assertLess(len(result.stdout), 4096)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])["probe_stage"], "invalid_context")


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = WORKER.load_protocol()

    def setUp(self):
        self.lib.budget.configure(30)

    def test_vvp_selected_channel_and_native_auth_are_explicit(self):
        import hashlib
        packet = WORKER._vvp(self.lib, context(), 0x11FF, 3)
        self.assertEqual(len(packet), 60)
        self.assertEqual(struct.unpack_from(">I", packet, 4)[0], 1)
        self.assertEqual(struct.unpack_from("<I", packet, 0x34)[0], 7)
        self.assertEqual(packet[0x39], 0)
        parts = ["admin", context()["device"]["hostKey"][:16], str(0x56565099), "3", str(0x11FF), "8", "meari.p2p.ppcs", "00000000000123456789"]
        self.assertEqual(packet[16:48], hashlib.md5("|".join(parts).encode()).hexdigest().encode())

    def test_native_stop_and_heartbeat_have_channel_and_zero_tail(self):
        for command in (0x12FF, 0x888E):
            packet = WORKER._vvp(self.lib, context(), command, 4)
            self.assertEqual(struct.unpack_from(">I", packet, 4)[0], 1)
            self.assertEqual(struct.unpack_from(">I", packet, 0x30)[0], 8)
            self.assertEqual(struct.unpack_from("<I", packet, 0x34)[0], 7)
            self.assertEqual(packet[0x38:0x3C], b"\x00" * 4)

    def test_fluent_modern_profile_changes_only_start_live_quality_byte(self):
        raw = context()
        raw["device"]["capability"] = {"caps": {"bps2": {
            "2": "2560x1440@20", "0": "640x360@15", "1": "1920x1080@20",
        }}}
        baseline = WORKER._vvp(self.lib, raw, 0x11FF, 3)
        raw["stream_quality"] = "fluent"
        expected = bytearray(baseline)
        expected[0x38] = 100
        self.assertEqual(WORKER._vvp(self.lib, raw, 0x11FF, 3), bytes(expected))
        for command in (0x12FF, 0x888E):
            packet = WORKER._vvp(self.lib, raw, command, 4)
            self.assertEqual(packet[0x34:0x3C], b"\x07\x00\x00\x00" + b"\x00" * 4)

    def test_fluent_uses_only_known_advertised_profiles(self):
        cases = (
            ({"bps2": {"2": "2560x1440"}}, 0),
            ({"bps2": {"3": "3840x2160"}}, 0),
            ({"bps": (1 << 1) | (1 << 2) | (1 << 5)}, 2),
            ({"bps": (1 << 1) | (1 << 5)}, 1),
            ({"bps": 1 << 3}, 3),
            ({"bps": 1 << 4}, 0),
            ({"vst": 1, "bps2": {"0": "640x360"}, "bps": 6}, 0),
            ({"bps2": {"99": "unknown"}, "bps": 6}, 0),
            ({"bps2": {"0": {}}, "bps": 6}, 0),
            ({"bps": True}, 0), ({"bps": 1}, 0), ({"bps": -1}, 0),
            ({"bps": "6"}, 0), ({}, 0),
        )
        for caps, expected in cases:
            with self.subTest(caps=caps):
                raw = {**context(), "stream_quality": "fluent"}
                raw["device"]["capability"] = {"caps": caps}
                self.assertEqual(WORKER._live_stream_id(raw), expected)
                self.assertEqual(WORKER._vvp(self.lib, raw, 0x11FF, 0)[0x38], expected)
                raw["stream_quality"] = "default"
                self.assertEqual(WORKER._live_stream_id(raw), 0)

    def test_native_auth_long_host_key_fixed_vector(self):
        raw = context()
        raw["device"]["hostKey"] = "0123456789abcdef0123456789abcdefMORE"
        self.assertEqual(WORKER._vvp(self.lib, raw, 0x11FF, 7)[16:48], b"6638dffbf2f40f17e706e8cc5107eef1")

    def test_h264_requires_keyframe_and_both_parameter_sets(self):
        observation = WORKER.Observation(self.lib)
        self.assertFalse(observation.feed(frame(b"\x00\x00\x00\x01\x65\x88\x84")))
        self.assertFalse(observation.keyframe_received)
        self.assertTrue(observation.feed(frame(H264)))
        self.assertEqual(observation.codec, "h264")
        self.assertTrue(observation.parameter_sets_received)
        self.assertFalse(observation.summary()["decoded"])

    def test_hevc_requires_vps_sps_pps_and_encoded_key_nal(self):
        observation = WORKER.Observation(self.lib)
        missing_pps = HEVC.replace(b"\x00\x00\x00\x01\x44\x01\x80\x80", b"")
        self.assertFalse(observation.feed(frame(missing_pps)))
        self.assertFalse(observation.parameter_sets_received)
        self.assertTrue(observation.feed(frame(HEVC)))
        self.assertEqual(observation.codec, "hevc")

    def test_header_only_nals_and_forbidden_bits_cannot_verify_keyframe(self):
        observation = WORKER.Observation(self.lib)
        empty = b"\x00\x00\x01\x67\x00\x00\x01\x68\x00\x00\x01\x65"
        self.assertFalse(observation.feed(frame(empty)))
        self.assertFalse(observation.parameter_sets_received)
        invalid = H264.replace(b"\x67\x64",b"\xe7\x64")
        self.assertFalse(observation.feed(frame(invalid)))
        self.assertFalse(observation.parameter_sets_received)

    def test_hevc_cra_and_bla_are_random_access_keyframes(self):
        for kind in (16,17,18,19,20,21):
            observation = WORKER.Observation(self.lib)
            payload = HEVC.replace(bytes([19<<1,1]),bytes([kind<<1,1]))
            self.assertTrue(observation.feed(frame(payload)))

    def test_parameter_sets_can_arrive_in_separate_frames(self):
        for payload in (H264, HEVC):
            observation = WORKER.Observation(self.lib)
            units = self.lib.codecs.annexb_units(payload)
            for unit in units[:-1]:
                self.assertFalse(observation.feed(frame(unit)))
            self.assertTrue(observation.feed(frame(units[-1])))

    def test_vendor_iframe_flag_is_insufficient_for_success(self):
        observation = WORKER.Observation(self.lib)
        self.assertFalse(observation.feed(frame(H264.replace(b"\x65\x88\x84\x21", b"\x61\x88\x84\x21"))))
        self.assertTrue(observation.parameter_sets_received)
        self.assertFalse(observation.keyframe_received)

    def test_slow_tcp_fragments_share_one_frame_deadline(self):
        from vendor import msgsvr_codec
        clock = [0.0]

        class SlowSocket(TimeoutSocket):
            def recv(self, size):
                clock[0] += 0.4
                return b"\xe6"

        sock = SlowSocket()
        sock.timeout = 1
        with patch.object(msgsvr_codec.time, "monotonic", side_effect=lambda: clock[0]):
            self.lib.budget.configure(30)
            with self.assertRaises(TimeoutError):
                msgsvr_codec.recv_frame(sock)
        self.assertLessEqual(clock[0], 1.3)

    def test_unrelated_stun_packets_do_not_extend_transaction(self):
        from vendor import turn_client
        clock = [0.0]
        turn = turn_client.TurnClient("192.0.2.1", 9100, "private-user", "private-pass")
        turn._send = lambda value: None
        calls = []

        def receive(timeout):
            calls.append(timeout)
            clock[0] += 0.4
            return b"unrelated"

        turn._recv = receive
        with patch.object(turn_client.time, "monotonic", side_effect=lambda: clock[0]):
            self.lib.budget.configure(30)
            self.assertIsNone(turn._stun_request(turn_client.ALLOCATE_REQUEST))
        self.assertLessEqual(len(calls), 8)
        self.assertLessEqual(clock[0], 3.3)

    def test_no_geographic_root_fallback(self):
        from vendor.p2p_streamer import root_discovery
        self.assertEqual(root_discovery._domains(None, None), [])
        self.assertEqual(root_discovery._domains(None, "https://openapi-cn.example.invalid"), ["cn.example.invalid"])

    def test_candidate_and_reassembly_bounds(self):
        rows = [{"ip": "192.0.2.1", "port": index + 1} for index in range(50)]
        self.assertEqual(len(WORKER._candidates(rows)), WORKER.MAX_CANDIDATES)
        self.assertEqual(WORKER._candidates([{"ip": "127.0.0.1", "port": 80}]), [])
        kcp = self.lib.KcpTunnel(lambda value: None)
        kcp.recv_frag_buf = [b"x" * (WORKER.MAX_FRAME_BYTES + 1)]
        with self.assertRaises(ValueError):
            WORKER._bounded_kcp(kcp)

    def test_fake_transport_reaches_encoded_keyframe_and_cleans_sockets(self):
        from vendor.kcp_tunnel import build_iva_data_frame
        sent = []
        instances = []
        queried = []

        class Signaling:
            heartbeat_interval_s = 30

            def __init__(self, *args, **kwargs):
                self.sock = TimeoutSocket()
                self.closed = False
                instances.append(self)

            def connect(self, **kwargs):
                pass

            def register(self, **kwargs):
                self.identity = kwargs

            def webrtc_hello_full(self):
                pass

            def query_device_status(self, uuid):
                queried.append(uuid)
                return {"status": "online"}

            def request_coturn(self, uuid):
                return {"coturn_ip": "192.0.2.1", "coturn_port": 9100, "username": "SECRET", "pwd": "SECRET"}

            def send_offer(self, uuid, sdp):
                return {"sdp": "a=ice-ufrag:camera\na=ice-pwd:private\nm=audio 9101 RTP / AVP 0\nc=IN IP4 192.0.2.2\n"}

            def recv_webrtc_content(self):
                raise socket.timeout()

            def send_candidate_complete(self, uuid):
                return {}

            def close(self):
                self.closed = True

        class Turn:
            relay_ip = "192.0.2.3"
            relay_port = 9102

            def __init__(self, *args):
                self.closed = False
                instances.append(self)

            def connect(self):
                pass

            def allocate(self):
                return True

            def create_permission(self, ip):
                return True

            def channel_bind(self, ip, port):
                return True

            def send_to_peer(self, ip, port, data):
                sent.append(data)

            def close(self):
                self.closed = True

        fake = SimpleNamespace(**vars(self.lib))
        fake.MsgSvrClient, fake.TurnClient = Signaling, Turn
        fake.discover_msgsvr_endpoints = lambda **kwargs: [("192.0.2.1", 28974)]
        payload = build_iva_data_frame(frame(H264))
        fake.recv_peer_packets = lambda *args, **kwargs: [SimpleNamespace(data=payload, peer=("192.0.2.2", 9101), source=("192.0.2.1", 9100), via_turn=True, stun=None)]
        stages = []
        outcome, summary = WORKER.run_probe(context(), stages.append, library=fake)
        self.assertEqual(outcome, "video_keyframe_received")
        self.assertTrue(summary["keyframe_received"])
        self.assertFalse(summary["decoded"])
        self.assertEqual(queried, ["0000000123456789"])
        self.assertTrue(all(instance.closed for instance in instances))
        self.assertEqual(instances[0].identity["brand"], "PRIVATE-BRAND")
        self.assertIn("signaling_registered", stages)
        vvp = [packet for packet in sent if b"\x56\x56\x50\x99" in packet]
        self.assertGreaterEqual(len(vvp), 2)  # live-start and best effort stop
        self.assertNotIn("SECRET", json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
