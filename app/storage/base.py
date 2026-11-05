from abc import ABC, abstractmethod


class EvidenceStorage(ABC):
    @abstractmethod
    def save(self, key: str, content: bytes, content_type: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def read(self, key: str) -> bytes:
        raise NotImplementedError

    @abstractmethod
    def delete(self, key: str) -> None:
        raise NotImplementedError
