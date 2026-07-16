# Import Request to access request state
from fastapi import Request

# Import HTTPException for access denial
from fastapi import HTTPException, status


def require_admin(request: Request) -> None:
    """
    This dependency ensures the user is an admin.

    Later you can replace this with:
    - JWT role checking
    - API key validation
    - database role lookup
    """

    # Attempt to read user from request state
    user = getattr(request.state, "user", None)

    # If user does not exist OR role is not admin → block request
    if not user or getattr(user, "role", None) != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required."
        )
