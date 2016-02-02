from django.db import models
from django.conf import settings

from djangae.db import transaction
from djangae.fields import JSONField, RelatedSetField, SetField

from hashlib import md5


class ScanMarshall(models.Model):
    files_left_to_process = models.PositiveIntegerField(default=0)

    def save(self, *args, **kwargs):
        self.pk = 1 # Singleton

        # You can create the ScanMarshall object without a count, but once
        # you save it again without one it means we reached the end of processing
        # so we remove the marshal object
        if self.files_left_to_process == 0 and not self._state.adding:
            self.delete()
        else:
            return super(ScanMarshall, self).save(*args, **kwargs)

    class Meta:
        app_label = "fluent"


class Translation(models.Model):
    master_translation = models.ForeignKey("fluent.MasterTranslation", editable=False, related_name="+")
    language_code = models.CharField(max_length=8, blank=False)

    text = models.TextField(blank=False) # This is the translated singular
    plural_texts = JSONField(blank=True) # These are the various plural translations depending on the language

    denorm_master_text = models.TextField(editable=False)
    denorm_master_hint = models.CharField(max_length=500, editable=False)
    denorm_master_language = models.CharField(max_length=8, editable=False)

    master_text_hint_hash = models.CharField(max_length=64)

    class Meta:
        app_label = "fluent"

    @staticmethod
    def generate_hash(master_text, master_hint):
        assert master_text
        assert master_hint is not None

        result = md5()
        for x in (master_text, master_hint):
            result.update(x)
        return result.hexdigest()

    def save(self, *args, **kwargs):
        assert self.language_code
        assert self.master_translation_id
        assert self.text

        self.denorm_master_text = self.master_translation.text
        self.denorm_master_hint = self.master_translation.hint
        self.denorm_master_language = self.master_translation.language_code

        # 'o' or ONE is the singular form, so if plurals
        # haven't been populated, make sure we always have that one
        if not self.plural_texts:
            self.plural_texts['o'] = self.text

        # For querying (you can't query for text on the datastore)
        self.master_text_hint_hash = Translation.generate_hash(
            self.denorm_master_text,
            self.denorm_master_hint
        )

        super(Translation, self).save(*args, **kwargs)


class MasterTranslation(models.Model):
    id = models.CharField(max_length=64, primary_key=True)

    text = models.TextField()
    plural_text = models.TextField(blank=True)
    hint = models.CharField(max_length=500, default="", blank=True)

    language_code = models.CharField(
        max_length=8,
        choices=settings.LANGUAGES,
        default=settings.LANGUAGE_CODE
    )

    translations_by_language_code = JSONField()
    translations = RelatedSetField(Translation)

    # Was this master translation updated or created by make messages?
    used_in_code_or_templates = models.BooleanField(default=False, blank=True, editable=False)

    # Were any groups specified in the trans tags?
    used_by_groups_in_code_or_templates = SetField(models.CharField(max_length=64), blank=True)

    # Record the ID of the last scan which updated this instance (if any)
    last_updated_by_scan_uuid = models.CharField(max_length=64, blank=True, default="")

    def __unicode__(self):
        return u"{} ({})".format(self.text, self.language_code)

    @classmethod
    def find_by_group(cls, group_name):
        from .fields import find_all_translatable_fields
        translatable_fields = find_all_translatable_fields(with_group=group_name)

        # Go through all Translatable(Char|Text)Fields or TextFields marked with the specified group and get
        # all the master translation IDs which are set to them
        master_translation_ids = []
        for model, field in translatable_fields:
            master_translation_ids.extend(
                model.objects.values_list(field.attname, flat=True)
            )
            master_translation_ids = list(set(master_translation_ids))

        # Now get all the master translations with a group specified in the templates
        master_translation_ids.extend(
            list(MasterTranslation.objects.filter(used_by_groups_in_code_or_templates=group_name).values_list("pk", flat=True))
        )

        # Make sure master translation ids don't include None values or duplicates
        master_translation_ids = set(master_translation_ids)
        master_translation_ids = master_translation_ids - {None}
        # Return them all!
        return MasterTranslation.objects.filter(pk__in=master_translation_ids)

    @staticmethod
    def generate_key(text, hint, language_code):
        assert text
        assert hint is not None
        assert language_code

        result = md5()
        for x in (text, hint, language_code):
            result.update(x)
        return result.hexdigest()

    def save(self, *args, **kwargs):
        assert self.text
        assert self.language_code

        # Generate the appropriate key on creation
        if self._state.adding:
            self.pk = MasterTranslation.generate_key(
                self.text, self.hint, self.language_code
            )

        # If there was no plural text specified, just use the default text
        if not self.plural_text:
            self.plural_text = self.text

        # If we are adding for the first time, then create a counterpart
        # translation for the master language
        if self._state.adding:
            with transaction.atomic(xg=True):
                new_trans = Translation.objects.create(
                    master_translation=self,
                    language_code=self.language_code,
                    text=self.text,
                    denorm_master_text=self.text,
                    denorm_master_hint=self.hint
                )
                self.translations_by_language_code[self.language_code] = new_trans.pk
                self.translations.add(new_trans)

                return super(MasterTranslation, self).save(*args, **kwargs)
        else:
            # Otherwise just do a normal save
            return super(MasterTranslation, self).save(*args, **kwargs)

    def create_or_update_translation(self, language_code, singular_text, plural_texts=None):
        with transaction.atomic(xg=True):
            self.refresh_from_db()

            if language_code in self.translations_by_language_code:
                # We already have a translation for this language, update it!
                trans = Translation.objects.get(pk=self.translations_by_language_code[language_code])
                trans.text = singular_text
                trans.plural_texts = plural_texts or {}
                trans.save()
            else:
                # OK, create the translation object and add it to the respective fields
                trans = Translation.objects.create(
                    master_translation_id=self.pk,
                    language_code=language_code,
                    text=singular_text,
                    plural_texts=plural_texts or {},
                    denorm_master_hint=self.hint,
                    denorm_master_text=self.text
                )

                self.translations_by_language_code[language_code] = trans.pk
                self.translations.add(trans)
                self.save()

    class Meta:
        app_label = "fluent"
