def sign_in(username, password, users):
    user = users.get(username)
    if user is None or not verify_password(password, user.password_hash):
        raise PermissionError("Invalid credentials")
    return create_session(user.id)
