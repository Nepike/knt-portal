import threading

from storages.backends.s3 import S3Storage

from .storage import content_type

_connecting = threading.Lock()


class R2Storage(S3Storage):
    @property
    def connection(self):
        """Одно соединение boto3 на процесс, а не на поток, как у django-storages.

        Синхронная вьюха под ASGI идёт в новом потоке на каждый запрос (Django оборачивает
        запрос в ThreadSensitiveContext, asgiref заводит под него свой поток), и
        `threading.local` родителя создавал сессию boto3 заново почти на каждую картинку:
        ~240 мс процессора и рост воркера web с ~130 до ~360 МБ за пару часов (замер 7 ноя 2026).

        Делить безопасно: всё, что через него идёт, — это `.meta.client`, а клиент boto3
        потокобезопасен; объект бакета родитель и так держит один на все потоки.
        """
        shared = self.__dict__.get("_shared_connection")
        if shared is None:
            with _connecting:
                shared = self.__dict__.get("_shared_connection")
                if shared is None:
                    shared = self._shared_connection = super().connection
        return shared

    def __getstate__(self):
        # Соединение не переживает pickle; родитель так же выбрасывает своё.
        state = super().__getstate__()
        state.pop("_shared_connection", None)
        return state

    def get_object_parameters(self, name):
        """Тип объекта назначаем сами, по имени.

        Без этого django-storages берёт его из заголовка, который прислал браузер
        загрузившего, — то есть чем файл объявится остальным, решал бы сам загрузивший:
        на попадании в кеш nginx до браузера доезжает именно тип из объекта. Заодно
        с готовым типом storages не дописывает ContentEncoding по хвосту имени (.svgz, .gz).
        """
        return {**super().get_object_parameters(name), "ContentType": content_type(name)}
