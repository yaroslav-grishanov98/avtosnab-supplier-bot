from zoneinfo import available_timezones

from rest_framework import serializers

_PASSWORD_HELP_TEXT = "Пароль, минимум 4 символа. Может состоять из букв и/или цифр."


class SupplierRegisterSerializer(serializers.Serializer):
    email = serializers.EmailField(help_text="Email — логин поставщика, должен быть уникальным в системе.")
    password = serializers.CharField(
        min_length=4, trim_whitespace=False, write_only=True, help_text=_PASSWORD_HELP_TEXT
    )
    full_name = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="ФИО контактного лица. Необязательно, по умолчанию используется email.",
    )


class SupplierLoginSerializer(serializers.Serializer):
    email = serializers.EmailField(help_text="Email, указанный при регистрации.")
    password = serializers.CharField(trim_whitespace=False, write_only=True, help_text="Пароль поставщика.")


class ProfileUpdateSerializer(serializers.Serializer):
    """PATCH /api/v1/auth/me — role=supplier или role=venue_user. Все поля
    необязательны, передаются только те, что реально меняются (partial update)."""

    first_name = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, help_text="Имя.")
    last_name = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, help_text="Фамилия.")
    position = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Должность — свободный текст."
    )
    phone = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Контактный телефон."
    )
    timezone = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="IANA-имя часового пояса (например Europe/Moscow). Фронт определяет через браузер, "
        "поле остаётся редактируемым.",
    )
    email = serializers.EmailField(required=False, help_text="Email — логин. Должен быть уникален в системе.")

    def validate_timezone(self, value):
        if value and value not in available_timezones():
            raise serializers.ValidationError("Неизвестный часовой пояс — ожидается IANA-имя, например Europe/Moscow.")
        return value


class BootstrapAdminUserSerializer(serializers.Serializer):
    username = serializers.CharField(
        trim_whitespace=True,
        help_text="Технический логин аккаунта (для Django admin/внутреннего учёта) — входить нужно по email.",
    )
    password = serializers.CharField(
        min_length=4, trim_whitespace=False, write_only=True, help_text=_PASSWORD_HELP_TEXT
    )
    full_name = serializers.CharField(trim_whitespace=True, help_text="ФИО сотрудника.")
    email = serializers.EmailField(help_text="Контактный email — обязателен и должен быть уникален в системе.")
    phone = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, help_text="Контактный телефон. Необязательно."
    )


class VenueRegisterSerializer(serializers.Serializer):
    email = serializers.EmailField(help_text="Email — логин заведения/покупателя, должен быть уникальным в системе.")
    password = serializers.CharField(
        min_length=4, trim_whitespace=False, write_only=True, help_text=_PASSWORD_HELP_TEXT
    )
    full_name = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="ФИО контактного лица. Необязательно, по умолчанию используется email.",
    )


class VenueLoginSerializer(serializers.Serializer):
    email = serializers.EmailField(help_text="Email аккаунта заведения/закупщика.")
    password = serializers.CharField(trim_whitespace=False, write_only=True, help_text="Пароль от аккаунта.")


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField(
        help_text="Email аккаунта поставщика или самостоятельно зарегистрированного заведения, "
        "на который нужно выслать ссылку сброса."
    )


class PasswordResetConfirmSerializer(serializers.Serializer):
    new_password = serializers.CharField(
        min_length=4, trim_whitespace=False, write_only=True, help_text=_PASSWORD_HELP_TEXT
    )
    confirm_password = serializers.CharField(
        min_length=4, trim_whitespace=False, write_only=True, help_text="Повтор нового пароля — должен совпадать."
    )

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "Пароли не совпадают."})
        return attrs
