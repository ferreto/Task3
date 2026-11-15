from enum import IntEnum


class CompanyStatus(IntEnum):
    OK = 0
    NO_IMPRESSUM_LINK_FOUND = 1
    PARSING_FAILED = 2
    ADDRESS_INCOMPLETE = 3
    ERROR = 4
    NO_WEBSITE = 5

    @property
    def label(self) -> str:
        return _LABELS[self]

    @classmethod
    def from_value(cls, value):
        if value is None:
            return None
        return cls(int(value))


_LABELS = {
    CompanyStatus.OK: "ok",
    CompanyStatus.NO_IMPRESSUM_LINK_FOUND: "no_impressum_link_found",
    CompanyStatus.PARSING_FAILED: "parsing_failed",
    CompanyStatus.ADDRESS_INCOMPLETE: "address_incomplete",
    CompanyStatus.ERROR: "error",
    CompanyStatus.NO_WEBSITE: "no_website",
}

PENDING = None


def is_ok(status) -> bool:
    return status is not None and int(status) == CompanyStatus.OK


def label(status) -> str:
    if status is None:
        return "pending"
    return CompanyStatus.from_value(status).label
