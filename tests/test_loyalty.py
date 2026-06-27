async def test_loyalty_me_requires_auth(client):
    response = await client.get("/api/v1/loyalty/me")
    assert response.status_code == 401


def test_redeem_points_request_does_not_accept_user_id():
    from app.modules.loyalty.account.schemas import RedeemPointsRequest

    assert "user_id" not in RedeemPointsRequest.model_fields


def test_loyalty_rule_create_requires_type_specific_fields():
    import pytest
    from pydantic import ValidationError

    from app.modules.loyalty.config.schemas import LoyaltyRuleCreate

    with pytest.raises(ValidationError):
        LoyaltyRuleCreate(
            name="Double categorie",
            rule_type="category_multiplier",
            multiplier="2.0",
        )


def test_loyalty_reward_create_requires_type_specific_fields():
    import pytest
    from pydantic import ValidationError

    from app.modules.loyalty.config.schemas import LoyaltyRewardCreate

    with pytest.raises(ValidationError):
        LoyaltyRewardCreate(
            name="Pizza offerte",
            reward_type="free_product",
            points_required=100,
        )


def test_loyalty_preview_exposes_multiplier_cap_fields():
    from app.modules.loyalty.config.schemas import LoyaltyPointsPreview

    preview = LoyaltyPointsPreview(
        base_points=10,
        bonus_points=190,
        total_points=200,
        applied_rules=["bonus"],
        total_multiplier="20.0",
        max_multiplier="20.0",
        multiplier_was_capped=True,
    )

    assert preview.multiplier_was_capped is True
    assert str(preview.max_multiplier) == "20.0"
