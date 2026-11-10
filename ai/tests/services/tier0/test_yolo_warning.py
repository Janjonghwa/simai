import logging

from services.tier0.yolo_detector import _HalfDeprecationOnce


def test_only_half_deprecation_is_logged_once():
    filt = _HalfDeprecationOnce()
    def record(message):
        return logging.LogRecord("ultralytics", logging.WARNING, "", 1, message, (), None)

    warning = record("WARNING 'half' is deprecated and will be removed in the future. Use 'quantize' instead.")
    assert filt.filter(warning)
    assert not filt.filter(warning)
    assert filt.filter(record("another warning"))
