# Earnings Transcript Summarizer

A simple script to systematically run earnings transcripts through LLMs (Google Gemini) to generate summaries and compile them into a master PDF for each company.

## Features

-   **Automated Summarization**: Uses Google Gemini to generate 1-2 page summaries of earnings call transcripts.
-   **Strategic Analysis**: Performs a "Say-Do" analysis comparing management guidance from previous quarters to actual results in subsequent quarters.
-   **Master PDF Compilation**: Merges cover pages, summaries, analysis, and full transcripts into a single, chronological PDF for each company.
-   **Smart Caching**: Avoids re-processing existing quarters (and costly API calls) by caching summaries and analyses.
-   **Table of Contents**: Generates a clickable Table of Contents for easy navigation within the master PDF.

## Usage

### Setup
1.  **Configure API Key**:
    *   Create a `.env` file in the project root.
    *   Add your Google Gemini API key:
        ```env
        GEMINI_API_KEY=your_api_key_here
        ```
2.  **Verify Models** (Optional):
    *   Run `python check_models.py` to verify your API key and see available Gemini models.

### Running the Processor
1.  Place your PDF earnings transcripts in the `transcripts_in` directory.
    *   Filename format: `Company_Qx_YYYY.pdf` (e.g., `Google_Q1_2024.pdf`).
2.  Run the script:
    ```bash
    python src/main.py
    ```
3.  The script will:
    *   Move processed files to `transcripts_processed`.
    *   Generate summaries, strategic analysis, and cover pages in `temp`.
    *   Output the final master PDFs in `transcripts_master`.

## Requirements

-   Python 3.x
-   `google-generativeai`
-   `python-dotenv`
-   See `requirements.txt` for full dependencies.
-   Google Gemini API Key
