class DomainError(Exception):
    """Сообщение, которое можно показать пользователю."""


class AccessDenied(DomainError):
    def __init__(self) -> None:
        super().__init__("У вас нет доступа к этому действию.")


class Conflict(DomainError):
    def __init__(self) -> None:
        super().__init__("Интервал уже занят. Выберите другое время.")
