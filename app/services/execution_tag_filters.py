"""Pure execution-tag query predicates shared by screening and page filters."""


def matches_execution_tag_filter(tags: list[str] | None, execution_tag_filter: str) -> bool:
    normalized = str(execution_tag_filter or "").strip().lower()
    if not normalized or normalized == "all":
        return True
    requested = [part.strip() for part in normalized.split(",") if part.strip() and part.strip() != "all"]
    if not requested:
        return True
    values = [str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()]
    return any(tag in values for tag in requested)


def excludes_execution_tag_filter(tags: list[str] | None, exclude_execution_tag_filter: str) -> bool:
    normalized = str(exclude_execution_tag_filter or "").strip().lower()
    if not normalized or normalized == "all":
        return True
    requested = [part.strip() for part in normalized.split(",") if part.strip() and part.strip() != "all"]
    if not requested:
        return True
    values = [str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()]
    return not any(tag in values for tag in requested)
