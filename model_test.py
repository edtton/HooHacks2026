import os
import time
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai.types import Tool, GenerateContentConfig, Part

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# 1. Setup paths and URLs
target_url = "https://www.hackfaxpatriothacks.org/"
guidelines_url = "https://guide.mlh.io/"
image_folder = Path("./instaloader_downloads/patriothacks")  # Your folder of JPEGs

# 2. Upload images to the Gemini File API
print("Uploading images...")
uploaded_files = []
for img_path in image_folder.glob("*.jpg"):  # Also catches .jpeg if you use *.jp*g
    # file = client.files.upload(path=img_path)
    file = client.files.upload(file=img_path)
    uploaded_files.append(file)
    print(f"Uploaded: {img_path.name}")

# Optional: Wait for files to be processed if they are very large
# (Usually instant for standard JPEGs)

# 3. Construct the multimodal prompt
prompt_text = (
    f"Please evaluate the hackathon website at {target_url} AND the provided "
    f"Instagram post images against the MLH guidelines found at {guidelines_url}. "
    "You are a first-time hacker. Based on the MLH guidelines, what questions "
    "might you be asking on the day of the event? Did you find the answers on "
    "either the website or within the Instagram content? "
    "Please provide a list of questions and where you found (or didn't find) the answers."
)

# 4. Generate content with text and multiple images
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=[
        prompt_text,
        *uploaded_files  # This unpacks all uploaded file objects into the request
    ],
    config=GenerateContentConfig(
        tools=[{"url_context": {}}],
    )
)

print("\n--- Analysis Results ---\n")
print(response.text)

# 5. Cleanup (Optional: deletes the files from the Gemini cloud)
for file in uploaded_files:
    client.files.delete(name=file.name)