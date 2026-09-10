from rest_framework import serializers


class MeSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField()
    is_superuser = serializers.BooleanField()
    role = serializers.SerializerMethodField()
    department = serializers.SerializerMethodField()

    def get_role(self, user):
        return getattr(user.profile, "role", None)

    def get_department(self, user):
        return getattr(user.profile, "department", "")
