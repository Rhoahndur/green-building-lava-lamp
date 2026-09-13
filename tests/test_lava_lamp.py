import contextlib
import io
import itertools
import unittest
from unittest.mock import MagicMock, patch

import lava_lamp


class LampTests(unittest.TestCase):
    def test_reproducible_changing_rgb_frames(self):
        first = list(itertools.islice(lava_lamp.frames(seed=140), 80))
        second = list(itertools.islice(lava_lamp.frames(seed=140), 80))
        self.assertEqual(first, second)
        self.assertGreater(len(set(first)), 70)
        self.assertTrue(all(isinstance(f, bytes) and len(f) == 459 for f in first))

    def test_stream_contract(self):
        connection = MagicMock()
        connection.getresponse.return_value.status = 204
        connection.getresponse.return_value.read.return_value = b""
        with patch("sys.argv", ["lava_lamp.py", "crisp-owl", "--frames", "2"]), \
                patch("lava_lamp.http.client.HTTPSConnection", return_value=connection), \
                patch("lava_lamp.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            lava_lamp.main()
        self.assertEqual(connection.request.call_count, 2)
        call = connection.request.call_args
        self.assertEqual(call.args, ("POST", "/api/i/crisp-owl/frame"))
        self.assertEqual(len(call.kwargs["body"]), 459)
        connection.close.assert_called_once()

    def test_reconnect_after_network_failure(self):
        broken, healthy = MagicMock(), MagicMock()
        broken.request.side_effect = OSError("connection lost")
        healthy.getresponse.return_value.status = 204
        healthy.getresponse.return_value.read.return_value = b""
        with patch("sys.argv", ["lava_lamp.py", "crisp-owl", "--frames", "1"]), \
                patch("lava_lamp.http.client.HTTPSConnection", side_effect=[broken, healthy]), \
                patch("lava_lamp.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            lava_lamp.main()
        broken.close.assert_called_once()
        healthy.request.assert_called_once()
        healthy.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
