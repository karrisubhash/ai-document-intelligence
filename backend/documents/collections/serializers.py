from rest_framework import serializers

from documents import authorization
from documents.models import Collection


class CollectionSerializer(serializers.ModelSerializer):
    documents = serializers.SerializerMethodField()
    document_count = serializers.SerializerMethodField()
    created_by = serializers.SlugRelatedField(slug_field="username", read_only=True)
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = Collection
        fields = [
            "id",
            "name",
            "description",
            "department",
            "documents",
            "document_count",
            "created_by",
            "can_manage",
            "created_at",
            "updated_at",
        ]
        # Department is decided by the view, like an upload's.
        read_only_fields = ["department", "created_by", "created_at", "updated_at"]
        # Uniqueness of (department, name) is the database's to enforce: the
        # views turn its IntegrityError into a 400. DRF's generated validator
        # cannot read a read-only department.
        validators = []

    def _visible(self, collection):
        # Only the members the CALLER may see. A curator in the department
        # sees them all; anyone else must not learn that a document they
        # cannot open exists, even by id.
        # Memoised on the instance: `documents` and `document_count` both
        # need it, and a listing would otherwise query twice per row.
        cached = getattr(collection, "_visible_members", None)
        if cached is None:
            request = self.context["request"]
            members = authorization.visible_documents(
                request.user, collection.documents.all()
            )
            cached = list(members.order_by("title", "id").values("id", "title"))
            collection._visible_members = cached
        return cached

    def get_documents(self, collection):
        return self._visible(collection)

    def get_document_count(self, collection):
        return len(self._visible(collection))

    def get_can_manage(self, collection):
        return authorization.can_manage_collection(
            self.context["request"].user, collection
        )


class CollectionCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    # Honoured for system administrators only; see CollectionViewSet.create.
    department = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("A collection needs a name.")
        return value


class MembershipSerializer(serializers.Serializer):
    document_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), min_length=1, max_length=500
    )
