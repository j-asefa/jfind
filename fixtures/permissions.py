def may_edit_document(user, document):
    # The user has already signed in. This only checks permission to edit.
    return user.id == document.owner_id or "editor" in user.roles
