from storages.backends.s3 import S3Storage

from .storage import content_type


class R2Storage(S3Storage):
    def get_object_parameters(self, name):
        """Тип объекта назначаем сами, по имени.

        Без этого django-storages берёт его из заголовка, который прислал браузер
        загрузившего, — то есть чем файл объявится остальным, решал бы сам загрузивший:
        на попадании в кеш nginx до браузера доезжает именно тип из объекта. Заодно
        с готовым типом storages не дописывает ContentEncoding по хвосту имени (.svgz, .gz).
        """
        return {**super().get_object_parameters(name), "ContentType": content_type(name)}
