import os
from pathlib import Path
from dotenv import load_dotenv
import uvicorn
load_dotenv(Path(__file__).resolve().parent/'.env')
if __name__=='__main__':
    uvicorn.run('agent:app',host=os.getenv('HOST','127.0.0.1'),port=int(os.getenv('PORT','8787')),reload=False)
