"""隐私：社工仅可查看自己负责片区且经授权的身份信息。"""

from src.models import PrivacyConsent
from conftest import time_windows


def _elder(service, make_elder):
    return make_elder(
        name="张桂芳",
        id_number="110101194001011234",
        phone="13812345678",
        address="东区幸福里3栋502室",
        windows=time_windows(0),
    )


def test_worker_sees_full_identity_in_own_area(service, make_elder):
    worker = service.register_worker("王社工", ["东区"])
    elder = _elder(service, make_elder)

    view = service.view_elder_identity(worker.id, elder.id)
    assert view == {
        "masked": False,
        "name": "张桂芳",
        "id_number": "110101194001011234",
        "phone": "13812345678",
        "address": "东区幸福里3栋502室",
        "area": "东区",
    }


def test_worker_outside_area_gets_masked_identity(service, make_elder):
    outsider = service.register_worker("赵社工", ["西区"])
    elder = _elder(service, make_elder)

    view = service.view_elder_identity(outsider.id, elder.id)
    assert view["masked"] is True
    assert view["name"] == "张**"
    assert "******" in view["id_number"]
    assert "****" in view["phone"]
    assert "502" not in view["address"]
    assert view["area"] == "东区"  # 片区可见，详细地址隐藏


def test_consent_revocation_masks_even_for_own_area(service, make_elder):
    worker = service.register_worker("王社工", ["东区"])
    elder = _elder(service, make_elder)
    service.update_consent(elder.id, PrivacyConsent(share_identity_with_worker=False))

    assert service.view_elder_identity(worker.id, elder.id)["masked"] is True


def test_admin_sees_full_identity(service, make_elder):
    elder = _elder(service, make_elder)
    view = service.view_elder_identity("任意账号", elder.id, admin=True)
    assert view["masked"] is False
    assert view["id_number"] == "110101194001011234"
