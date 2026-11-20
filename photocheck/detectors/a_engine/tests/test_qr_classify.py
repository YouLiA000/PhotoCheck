"""K-06 unit tests: 100% classification accuracy (a pure rule problem) plus three red lines -
no short-link expansion, no identity inference, no payload display.

The Chinese strings below are product-facing labels asserted against the UI contract.
"""

import pytest

from core.detect.qr_classify import classify, to_display_class


@pytest.mark.parametrize(
    ("payload", "subtype", "risk"),
    [
        ("https://u.wechat.com/EAxxxx", "qr_wechat_card", 3),
        ("BEGIN:VCARD\nVERSION:3.0\nFN:张三\nTEL:13800138000\nEND:VCARD", "qr_vcard", 3),
        ("MECARD:N:张三;TEL:13800138000;;", "qr_vcard", 3),
        ("wxp://f2f0abcdefg", "qr_wechat_pay", 3),
        ("https://qr.alipay.com/abc123", "qr_alipay", 3),
        ("alipays://platformapi/startapp?appId=20000067", "qr_alipay", 3),
        ("WIFI:S:MyHome;T:WPA;P:secret123;;", "qr_wifi", 3),
        ("https://example.com/article/42", "qr_link", 1),
        ("http://t.cn/abc", "qr_link", 1),
        ("hello world", "qr_unknown", 1),
        ("", "qr_unknown", 1),
    ],
)
def test_classify(payload, subtype, risk):
    cls = classify(payload)
    assert cls.subtype == subtype
    assert cls.risk == risk


def test_wifi_ssid_shown_but_not_password():
    cls = classify("WIFI:S:MyHome;T:WPA;P:secret123;;")
    assert cls.summary == "MyHome"
    assert "secret123" not in (cls.summary or "")
    assert "secret123" not in cls.note


def test_wifi_ssid_with_escape():
    cls = classify(r"WIFI:S:My\;Home;T:WPA;P:x;;")
    assert cls.summary == "My;Home"


def test_link_summary_is_domain_only():
    cls = classify("https://u.wechat.com/EAxxxx")
    assert cls.summary == "u.wechat.com"
    assert "EAxxxx" not in (cls.summary or "")


def test_short_link_domain_kept_and_not_expanded():
    cls = classify("http://t.cn/abc")
    assert cls.summary == "t.cn"
    assert "不展开" in cls.note


def test_no_identity_judgement():
    """Red line: never guess "whose card this is" - the note must not echo the name."""
    cls = classify("BEGIN:VCARD\nFN:张三\nTEL:13800138000\nEND:VCARD")
    assert "张三" not in cls.note
    assert cls.summary is None


@pytest.mark.parametrize(
    ("subtype", "display"),
    [
        ("qr_wechat_card", "名片 / 联系人"),
        ("qr_vcard", "名片 / 联系人"),
        ("qr_wechat_pay", "收款 / 支付"),
        ("qr_alipay", "收款 / 支付"),
        ("qr_wifi", "Wi-Fi"),
        ("qr_link", "普通链接"),
        ("qr_unknown", "未识别"),
        ("qr_geo", "未识别"),
        ("qr_code", "未识别"),
    ],
)
def test_display_class(subtype, display):
    assert to_display_class(subtype) == display


def test_geo_and_tel_classified_but_display_unknown():
    assert classify("geo:39.9,116.4").subtype == "qr_geo"
    assert classify("tel:+8613800138000").subtype == "qr_contact"
    assert to_display_class("qr_geo") == "未识别"


# --- spec-card risk values, locked ----------------------------------------
#
# These numbers are shared contract: the same int ends up in B's v4 record,
# C's metrics and the UI risk badge. A once changed five of them on the
# argument that they were internally inconsistent; that argument was right but
# the place was wrong, so they were reverted and the change was resubmitted as
# a spec-card revision (reports/A_规格卡修订建议_2026-11-07.md).
#
# The table lives here so that the next person who wants to change a risk score
# has to change a test named after the spec card, not just an integer in the
# detector. If the revision passes, update both together.
SPEC_CARD_RISK = {
    "qr_wechat_card": 3,   # K-06 "名片 / 联系人 = risk 3"
    "qr_vcard": 3,
    "qr_contact": 3,
    "qr_wechat_group": 3,  # not yet adjudicated; keep the card value
    "qr_geo": 3,           # not yet adjudicated; keep the card value
    "qr_wechat_pay": 3,    # K-06 "收款 / 支付"
    "qr_alipay": 3,
    "qr_wifi": 3,          # K-06 "Wi-Fi 密码"
    "qr_link": 1,          # K-10 "普通链接 = 1"
    "qr_code": 1,          # 1D barcode, K-10
    "qr_unknown": 1,       # K-06 "未识别 = risk 1"
}


@pytest.mark.parametrize(
    "payload,expected_subtype",
    [
        ("https://u.wechat.com/EAxxxx", "qr_wechat_card"),
        ("https://weixin.qq.com/g/Axxxxx", "qr_wechat_group"),
        ("wxp://f2f0xxxx", "qr_wechat_pay"),
        ("https://qr.alipay.com/abc123", "qr_alipay"),
        ("WIFI:S:Campus-Guest;T:WPA;P:abc;H:false;;", "qr_wifi"),
        ("BEGIN:VCARD\nVERSION:3.0\nFN:Zhang\nTEL:13800138000\nEND:VCARD", "qr_vcard"),
        ("tel:13800138000", "qr_contact"),
        ("geo:39.9,116.4", "qr_geo"),
        ("https://example.com/a", "qr_link"),
        ("hello world", "qr_unknown"),
        ("", "qr_unknown"),
    ],
)
def test_risk_matches_spec_card(payload, expected_subtype):
    """Every classified subtype must carry exactly the card's number."""
    c = classify(payload)
    assert c.subtype == expected_subtype, f"classification drifted: got {c.subtype}"
    assert c.risk == SPEC_CARD_RISK[c.subtype], (
        f"{c.subtype} risk is {c.risk}, spec card says {SPEC_CARD_RISK[c.subtype]}. "
        f"Changing a shared risk score needs a voted spec revision, not a code edit."
    )


def test_undecodable_qr_carries_the_same_risk_as_classified_unknown():
    """qr.py and qr_classify.py both mint `qr_unknown`; the two must agree.

    They are separate code paths (one for a payload that failed to classify, one
    for a box whose payload never decoded), so nothing but a test keeps them in
    sync - and they already drifted once.
    """
    from core.detect.qr import QRHit, build_detections

    hit = QRHit(payload=None, bbox=(10, 10, 100, 100), decodable=False, symbology="QRCODE")
    records = [d.to_dict() for d in build_detections([hit], "img-undecodable")]
    assert records[0]["subtype"] == "qr_unknown"
    assert records[0]["risk"] == SPEC_CARD_RISK["qr_unknown"]
