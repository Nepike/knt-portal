import threading

from storages.backends.s3 import S3Storage

from .storage import content_type

_connecting = threading.Lock()


class R2Storage(S3Storage):
    @property
    def connection(self):
        """Одно соединение boto3 на процесс, а не на поток, как у django-storages: под ASGI
        каждый запрос — новый поток, и сессия boto3 заводилась бы на каждую картинку
        (~240 мс и рост памяти воркера). Клиент boto3 потокобезопасен."""
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
        """Тип объекта — по имени (CONTENT_TYPES), а не из заголовка браузера загрузившего:
        на попадании в кеш nginx доезжает тип из объекта. Заодно storages не дописывает
        ContentEncoding по хвосту имени (.svgz, .gz)."""
        return {**super().get_object_parameters(name), "ContentType": content_type(name)}
