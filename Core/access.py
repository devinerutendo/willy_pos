def is_privileged_user(user):
    if not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False):
        return True
    return getattr(user, 'role', None) in {'ADMIN', 'MANAGER'}


def branch_scoped_queryset(queryset, user, branch_lookup='branch'):
    if not getattr(user, 'is_authenticated', False):
        return queryset.none()
    if is_privileged_user(user):
        return queryset

    branch_id = getattr(user, 'branch_id', None)
    if not branch_id:
        return queryset

    return queryset.filter(**{f'{branch_lookup}_id': branch_id})