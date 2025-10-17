from flask_restx import fields

account_money_fields = {
    "total_quota": fields.Float,
    "used_quota": fields.Float,
}
