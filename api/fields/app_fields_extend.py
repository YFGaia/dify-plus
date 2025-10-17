from flask_restx import fields

from libs.helper import AppIconUrlField

# ---------------- start sync app to
app_fields = {
    "id": fields.String,
    "name": fields.String,
    "mode": fields.String,
    "icon": fields.String,
    "icon_type": fields.String,
    "icon_url": AppIconUrlField,
    "icon_background": fields.String,
}


recommended_app_fields = {
    "app": fields.Nested(app_fields, attribute="app"),
    "app_id": fields.String,
    "description": fields.String(attribute="description"),
    "copyright": fields.String,
    "privacy_policy": fields.String,
    "custom_disclaimer": fields.String,
    "installed_id": fields.String,
    "category": fields.String,
    "position": fields.Integer,
    "is_listed": fields.Boolean,
}

recommended_app_list_fields = {
    "recommended_apps": fields.List(fields.Nested(recommended_app_fields)),
    "categories": fields.List(fields.String),
}

# ---------------- stop sync app to
