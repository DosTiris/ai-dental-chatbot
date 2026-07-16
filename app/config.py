import os  # Import os to read environment variables
from dotenv import load_dotenv  # Import load_dotenv to load variables from a .env file

load_dotenv()  # Load environment variables from .env into the process

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # Read OpenAI API key from environment
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()  # Read admin key from env for protecting admin endpoints

DATABASE_URL = os.getenv("DATABASE_URL")  # Read PostgreSQL connection string from environment

if not OPENAI_API_KEY:  # Check if OpenAI key is missing
    raise RuntimeError("OPENAI_API_KEY not set")  # Stop early with clear error

if not DATABASE_URL:  # Check if database URL is missing
    raise RuntimeError("DATABASE_URL not set")  # Stop early with clear error
