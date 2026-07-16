from pydantic import BaseModel  # Import BaseModel for validation
from typing import Optional  # Import Optional for optional fields
from pydantic import BaseModel # in app/schemas.py
from typing import Optional, Dict, Any # in app/schemas.py

class ChatRequest(BaseModel):  # Request body schema for /chat
    message: str  # User message text
    client_key: str  # Your per-office API key (from widget)
    visitor_id: Optional[str] = None  # Optional browser visitor ID
    conversation_id: Optional[str] = None  # Optional conversation ID to continue a session

class ChatResponse(BaseModel):  # Response schema for /chat
    reply: str  # Assistant reply
    conversation_id: str  # Conversation ID for follow-up messages
    meta: Optional[Dict[str, Any]] = None



    
