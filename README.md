# Earnings Transcript Summarizer

A simple script to systematically run earnings transcripts through LLMs (Google Gemini) to generate summaries and compile them into a master PDF for each company.

## Features

-   **Automated Summarization**: Uses Google Gemini 2.5 Pro to generate 1-2 page summaries of earnings call transcripts.
-   **Master PDF Compilation**: Merges cover pages, summaries, and full transcripts into a single, chronological PDF for each company.
-   **Smart Caching**: Avoids re-processing existing quarters by caching summaries and checking for processed files.
-   **Table of Contents**: Generates a clickable Table of Contents for easy navigation within the master PDF.

## Usage

1.  Place your PDF earnings transcripts in the `transcripts_in` directory.
    *   Filename format: `Company_Qx_YYYY.pdf` (e.g., `Google_Q1_2024.pdf`).
2.  Run the script:
    ```bash
    python src/main.py
    ```
3.  The script will:
    *   Move processed files to `transcripts_processed`.
    *   Generate summaries and cover pages in `temp`.
    *   Output the final master PDFs in `transcripts_master`.

## Requirements

-   Python 3.x
-   See `requirements.txt` for dependencies.
-   Google Gemini API Key (configured in `src/llm_client.py` or environment variables).
