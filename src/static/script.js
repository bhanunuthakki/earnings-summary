document.addEventListener('DOMContentLoaded', () => {
    const searchInput = document.getElementById('companySearch');
    const dropdownList = document.getElementById('dropdownList');
    const analyzeBtn = document.getElementById('analyzeBtn');
    const btnText = analyzeBtn.querySelector('.btn-text');
    const loader = analyzeBtn.querySelector('.loader');
    const pdfUploadInput = document.getElementById('pdfUploadInput');
    const uploadBtn = document.getElementById('uploadBtn');
    const resultsArea = document.getElementById('resultsArea');
    const quarterTemplate = document.getElementById('quarterTemplate');
    const downloadMasterBtn = document.getElementById('downloadMasterBtn');

    const helpBtn = document.querySelector('.help-btn');
    const tooltipContent = document.querySelector('.tooltip-content');



    // Tooltip logic
    if (helpBtn && tooltipContent) {
        helpBtn.addEventListener('mouseenter', () => tooltipContent.classList.remove('hidden'));
        helpBtn.addEventListener('mouseleave', () => tooltipContent.classList.add('hidden'));
    }

    let companiesData = [];
    let selectedCompany = null;

    // Fetch and populate companies
    async function fetchCompanies() {
        try {
            const res = await fetch('/api/companies');
            if (res.ok) {
                companiesData = await res.json();
                renderDropdown(companiesData);
            } else {
                dropdownList.innerHTML = '<div class="dropdown-status">Failed to load companies.</div>';
            }
        } catch (e) {
            console.error(e);
            dropdownList.innerHTML = '<div class="dropdown-status">Error connecting to server.</div>';
        }
    }

    function renderDropdown(list) {
        if (list.length === 0) {
            dropdownList.innerHTML = '<div class="dropdown-status">No companies found.</div>';
            return;
        }

        dropdownList.innerHTML = '';
        list.forEach(item => {
            const div = document.createElement('div');
            div.className = 'dropdown-item';

            const nameSpan = document.createElement('span');
            nameSpan.className = 'ticker-name';
            nameSpan.textContent = item.display_name || item.name || item.ticker;

            const rightContainer = document.createElement('div');
            rightContainer.className = 'dropdown-right-info';

            const badgeDiv = document.createElement('div');
            badgeDiv.className = 'ticker-badges';

            if (item.has_data) {
                // Inline status text
                const statusText = document.createElement('span');
                statusText.className = 'inline-status';
                statusText.style.color = 'var(--text-secondary)';
                statusText.style.marginRight = '10px';
                statusText.style.fontSize = '0.85rem';
                statusText.textContent = `Avail: ${item.latest_quarter}`;
                rightContainer.appendChild(statusText);

                const b1 = document.createElement('span');
                b1.className = 'badge has-data';
                b1.textContent = `${item.available_quarters.length} Qtrs`;
                badgeDiv.appendChild(b1);

                if (item.has_master_pdf) {
                    const b2 = document.createElement('span');
                    b2.className = 'badge';
                    b2.style.background = 'rgba(88, 166, 255, 0.2)';
                    b2.style.color = 'var(--accent-color)';
                    b2.textContent = 'PDF';
                    badgeDiv.appendChild(b2);
                }
            } else {
                const b = document.createElement('span');
                b.className = 'badge no-data';
                b.textContent = 'Unprocessed';
                badgeDiv.appendChild(b);
            }

            rightContainer.appendChild(badgeDiv);

            div.appendChild(nameSpan);
            div.appendChild(rightContainer);

            div.addEventListener('click', () => {
                selectCompany(item);
            });

            dropdownList.appendChild(div);
        });
    }

    function selectCompany(item) {
        selectedCompany = item;
        searchInput.value = item.ticker;
        dropdownList.classList.add('hidden');

        analyzeBtn.disabled = false;
        if (item.has_master_pdf) {
            downloadMasterBtn.disabled = false;
            downloadMasterBtn.classList.remove('disabled-visual');
        } else {
            downloadMasterBtn.disabled = true;
            downloadMasterBtn.classList.add('disabled-visual');
        }

        // If they have data, try to fetch analysis immediately to preview
        if (item.has_data) {
            document.getElementById('mainHeader').classList.add('hidden');
            fetchAnalysis(item.ticker);
        } else {
            resultsArea.innerHTML = '';
            resultsArea.classList.add('hidden');
        }
    }

    // Filter companies as user types
    searchInput.addEventListener('input', (e) => {
        const val = e.target.value.toLowerCase();

        if (val === '') {
            selectedCompany = null;
            analyzeBtn.disabled = true;
            downloadMasterBtn.disabled = true;
            downloadMasterBtn.classList.add('disabled-visual');
            resultsArea.classList.add('hidden');
            dropdownList.classList.add('hidden');
            return;
        }

        // Search both ticker and name
        const filtered = companiesData.filter(c => {
            const matchTicker = c.ticker && c.ticker.toLowerCase().includes(val);
            const matchName = c.name && c.name.toLowerCase().includes(val);
            return matchTicker || matchName;
        });

        // Optimize rendering by taking max 150 items so the DOM does not freeze on 'a'
        renderDropdown(filtered.slice(0, 150));

        // Allow manual entry
        if (val.trim() !== '') {
            analyzeBtn.disabled = false;
            
            const exactMatch = companiesData.find(c => c.ticker.toLowerCase() === val.trim());
            if (exactMatch) {
                selectedCompany = exactMatch;
            } else {
                const manualTicker = val.trim().toUpperCase();
                selectedCompany = { 
                    ticker: manualTicker, 
                    name: manualTicker, 
                    has_data: false, 
                    has_master_pdf: false 
                };
            }
        }

        // Show dropdown
        dropdownList.classList.remove('hidden');
    });

    // Handle Enter key for manual entry
    searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            dropdownList.classList.add('hidden');
            if (!analyzeBtn.disabled) {
                analyzeBtn.click();
            }
        }
    });

    // Handle clicks outside the search box to close dropdown
    document.addEventListener('click', (e) => {
        if (!searchInput.contains(e.target) && !dropdownList.contains(e.target)) {
            dropdownList.classList.add('hidden');
        }
    });

    const fetchSourceSelect = document.getElementById('fetchSourceSelect');
    let pollInterval = null;

    // Trigger Pipeline
    analyzeBtn.addEventListener('click', async () => {
        if (!selectedCompany) return;

        analyzeBtn.disabled = true;
        if (btnText) btnText.classList.add('hidden');
        loader.classList.remove('hidden');

        const source = fetchSourceSelect ? fetchSourceSelect.value : 'fmp';
        
        const statusContainer = document.getElementById('pipelineStatus');
        const statusText = document.getElementById('statusText');
        const statusIcon = document.getElementById('statusIcon');
        
        if (statusContainer) {
            statusContainer.classList.remove('hidden');
            statusText.textContent = 'Initializing pipeline...';
            statusText.style.color = 'inherit';
            statusIcon.className = 'loader';
        }

        try {
            const res = await fetch('/api/process', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ company: selectedCompany.ticker, source: source })
            });
            const data = await res.json();

            // Start polling
            if (pollInterval) clearInterval(pollInterval);
            pollInterval = setInterval(async () => {
                try {
                    const statusRes = await fetch(`/api/status/${selectedCompany.ticker}`);
                    if (statusRes.ok) {
                        const statusData = await statusRes.json();
                        if (statusContainer) {
                            statusText.textContent = statusData.step || 'Processing...';
                        }
                        
                        if (statusData.status === 'completed') {
                            clearInterval(pollInterval);
                            analyzeBtn.disabled = false;
                            if (btnText) btnText.classList.remove('hidden');
                            loader.classList.add('hidden');
                            if (statusContainer) {
                                statusText.textContent = 'Analysis complete!';
                                statusIcon.className = '';
                                setTimeout(() => { statusContainer.classList.add('hidden'); }, 4000);
                            }
                            fetchAnalysis(selectedCompany.ticker);
                        } else if (statusData.status === 'failed') {
                            clearInterval(pollInterval);
                            analyzeBtn.disabled = false;
                            if (btnText) btnText.classList.remove('hidden');
                            loader.classList.add('hidden');
                            if (statusContainer) {
                                statusText.textContent = `Failed: ${statusData.error || statusData.step}`;
                                statusText.style.color = '#ff6b6b';
                                statusIcon.className = '';
                            }
                        }
                    }
                } catch (e) {
                    console.error("Polling error", e);
                }
            }, 2000);

        } catch (e) {
            console.error(e);
            alert("Error triggering process.");
            analyzeBtn.disabled = false;
            if (btnText) btnText.classList.remove('hidden');
            loader.classList.add('hidden');
            if (statusContainer) statusContainer.classList.add('hidden');
        }
    });

    // Handle Master Download
    downloadMasterBtn.addEventListener('click', () => {
        if (!selectedCompany) return;
        window.open(`/api/download/${selectedCompany.ticker}/master`, '_blank');
    });

    // Upload PDF Event
    pdfUploadInput.addEventListener('change', async (e) => {
        if (!e.target.files || e.target.files.length === 0) return;

        const formData = new FormData();
        Array.from(e.target.files).forEach(file => {
            formData.append('file', file);
        });
        if (selectedCompany) {
            formData.append('company', selectedCompany.ticker);
        }

        uploadBtn.classList.add('disabled-visual');

        try {
            const res = await fetch('/api/upload', {
                method: 'POST',
                body: formData
            });
            const data = await res.json();
            if (res.ok) {
                const numCompanies = data.companies ? data.companies.length : 0;

                if (numCompanies > 0) {
                    alert(`Upload successful! Detected ${numCompanies} compan${numCompanies > 1 ? 'ies' : 'y'} (${data.companies.join(', ')}). Pipeline processing has been queued or showed existing.`);

                    if (numCompanies === 1) {
                        const targetCompany = data.companies[0];
                        if (!selectedCompany || selectedCompany.ticker !== targetCompany) {
                            searchInput.value = targetCompany;
                            const found = companiesData.find(c => c.ticker === targetCompany);
                            if (found) {
                                selectedCompany = found;
                            } else {
                                selectedCompany = { ticker: targetCompany, name: targetCompany, has_master_pdf: false, has_data: true, latest_quarter: 'N/A', available_quarters: [] };
                            }
                        }
                        if (selectedCompany && selectedCompany.has_master_pdf) {
                            downloadMasterBtn.disabled = false;
                            downloadMasterBtn.classList.remove('disabled-visual');
                        }
                        fetchAnalysis(targetCompany);
                    } else {
                        searchInput.value = '';
                        selectedCompany = null;
                        analyzeBtn.disabled = true;
                        downloadMasterBtn.disabled = true;
                        downloadMasterBtn.classList.add('disabled-visual');

                        renderMultipleCompanies(data.companies);
                    }
                } else {
                    alert("Upload processed, but no valid companies detected.");
                }
            } else {
                alert("Upload failed: " + (data.error || "Unknown error"));
            }
        } catch (err) {
            console.error(err);
            alert("Error uploading file.");
        } finally {
            uploadBtn.classList.remove('disabled-visual');
            pdfUploadInput.value = '';
        }
    });

    async function renderMultipleCompanies(companyTickers) {
        document.getElementById('mainHeader').classList.add('hidden');
        resultsArea.innerHTML = '';
        resultsArea.classList.remove('hidden');
        resultsArea.style.flexDirection = 'column';
        resultsArea.style.overflowX = 'hidden';

        const loadingMsg = document.createElement('div');
        loadingMsg.className = 'status-placeholder';
        loadingMsg.textContent = 'Loading analysis data for multiple companies...';
        resultsArea.appendChild(loadingMsg);

        try {
            resultsArea.innerHTML = '';

            for (const ticker of companyTickers) {
                const res = await fetch(`/api/analysis/${ticker}`);
                if (res.ok) {
                    const data = await res.json();
                    if (data.length > 0) {
                        const card = createCompanyCardNode(ticker, data);
                        if (card) resultsArea.appendChild(card);
                    }
                }
            }

            if (resultsArea.children.length === 0) {
                resultsArea.innerHTML = '<div class="status-placeholder">No extracted analysis details found yet. They might still be processing.</div>';
            }
        } catch (e) {
            console.error(e);
            resultsArea.innerHTML = '<div class="status-placeholder">Error loading multiple companies.</div>';
        }
    }

    function createCompanyCardNode(companyTicker, quartersData) {
        const companyTemplate = document.getElementById('companyTemplate');
        if (!companyTemplate) return null;

        const clone = companyTemplate.content.cloneNode(true);
        const card = clone.querySelector('.company-card');
        const title = clone.querySelector('.c-title');
        const tabsContainer = clone.querySelector('.quarters-tabs');
        const contentArea = clone.querySelector('.company-content');

        let companyName = companyTicker;
        const found = companiesData.find(c => c.ticker === companyTicker);
        if (found) {
            companyName = found.display_name || (found.name && found.name !== companyTicker ? `${companyTicker} - ${found.name}` : companyTicker);
        }
        title.textContent = companyName;

        const limitedData = quartersData.slice(0, 4);

        if (limitedData.length === 0) {
            contentArea.innerHTML = '<div class="status-placeholder">No analysis details found.</div>';
            return card;
        }

        const quarterNodes = [];

        limitedData.forEach((qData, index) => {
            const tabBtn = document.createElement('button');
            tabBtn.className = `q-tab-btn ${index === 0 ? 'active' : ''}`;
            tabBtn.textContent = `${qData.quarter} ${qData.year}`;
            tabsContainer.appendChild(tabBtn);

            const qNode = createQuarterCardNode(qData, companyTicker);
            if (index !== 0) {
                qNode.classList.add('hidden');
            }
            contentArea.appendChild(qNode);
            quarterNodes.push({ btn: tabBtn, node: qNode });

            tabBtn.addEventListener('click', () => {
                quarterNodes.forEach(item => {
                    item.btn.classList.remove('active');
                    item.node.classList.add('hidden');
                });
                tabBtn.classList.add('active');
                qNode.classList.remove('hidden');
            });
        });

        return card;
    }

    // Render Quarters
    async function fetchAnalysis(ticker) {
        resultsArea.innerHTML = '';
        resultsArea.classList.remove('hidden');
        resultsArea.style.flexDirection = 'row';
        resultsArea.style.overflowX = 'auto';

        const loadingMsg = document.createElement('div');
        loadingMsg.className = 'status-placeholder';
        loadingMsg.textContent = 'Loading analysis data...';
        resultsArea.appendChild(loadingMsg);

        try {
            const res = await fetch(`/api/analysis/${ticker}`);
            if (res.ok) {
                const data = await res.json();
                resultsArea.innerHTML = ''; // clear loading

                if (data.length === 0) {
                    resultsArea.innerHTML = '<div class="status-placeholder">No analysis details found. The pipeline may have run but found no valid transcripts matching our criteria (some companies do not upload earnings transcripts to SEC EDGAR).</div>';
                    return;
                }

                const card = createCompanyCardNode(ticker, data);
                if (card) resultsArea.appendChild(card);
            } else {
                resultsArea.innerHTML = '<div class="status-placeholder">Error fetching analysis.</div>';
            }
        } catch (e) {
            console.error(e);
            resultsArea.innerHTML = '<div class="status-placeholder">Network error.</div>';
        }
    }

    function createQuarterCardNode(data, companyTicker) {
        const clone = quarterTemplate.content.cloneNode(true);
        const content = clone.querySelector('.quarter-content');

        // Tab elements
        const tabs = clone.querySelectorAll('.tab-btn');
        const saydoPanel = clone.querySelector('.saydo-panel');
        const summaryPanel = clone.querySelector('.summary-panel');
        const transcriptPanel = clone.querySelector('.transcript-panel');
        const downloadBtn = clone.querySelector('.download-tab-btn');
        let saydoHtml = data.saydo ? marked.parse(data.saydo) : '<p><em>No pairwise strategy analysis available for this quarter (requires previous quarter context to be parsed).</em></p>';
        saydoHtml = saydoHtml.replace(/(<strong>Performance Rating:<\/strong>\s*)(?:<strong>)?(MET|EXCEEDED|MISSED)(?:<\/strong>)?/gi, (match, prefix, rating) => {
            return `${prefix}<span class="rating-${rating.toLowerCase()}">${rating}</span>`;
        });

        // Convert Say vs Do to Table
        if (data.saydo) {
            const parser = new DOMParser();
            const doc = parser.parseFromString(saydoHtml, 'text/html');
            let sayHeading = null;
            let doHeading = null;

            doc.body.querySelectorAll('h2, h3').forEach(el => {
                const text = el.textContent.toLowerCase();
                if (text.includes('say') && text.includes('promise')) sayHeading = el;
                if (text.includes('do') && text.includes('reality')) doHeading = el;
            });

            if (sayHeading && doHeading) {
                const sayContent = [];
                let curr = sayHeading.nextElementSibling;
                while (curr && curr !== doHeading && !curr.matches('h2, h3')) {
                    sayContent.push(curr);
                    curr = curr.nextElementSibling;
                }

                const doContent = [];
                curr = doHeading.nextElementSibling;
                while (curr && !curr.matches('h2, h3')) {
                    doContent.push(curr);
                    curr = curr.nextElementSibling;
                }

                const table = document.createElement('table');
                table.className = 'say-do-table';
                table.innerHTML = `
                    <thead>
                        <tr>
                            <th>Say (The Promise)</th>
                            <th>Do (The Reality)</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td class="say-cell"></td>
                            <td class="do-cell"></td>
                        </tr>
                    </tbody>
                `;

                const sayCell = table.querySelector('.say-cell');
                const doCell = table.querySelector('.do-cell');

                sayContent.forEach(el => sayCell.appendChild(el));
                doContent.forEach(el => doCell.appendChild(el));

                sayHeading.parentNode.insertBefore(table, sayHeading);
                sayHeading.remove();
                doHeading.remove();

                saydoHtml = doc.body.innerHTML;
            }
        }

        saydoPanel.innerHTML = saydoHtml;

        summaryPanel.innerHTML = data.summary ? marked.parse(data.summary) : '<p><em>No summary available.</em></p>';

        if (data.pdf_available) {
            const pdfContainer = clone.querySelector('.pdf-container');
            const url = `/api/pdf/${companyTicker}/${data.quarter}/${data.year}`;
            pdfContainer.innerHTML = `<object data="${url}" type="application/pdf" width="100%" height="800px">
                <p>Unable to display PDF directly. <a href="${url}" target="_blank">Download PDF</a></p>
            </object>`;
        } else {
            const rawText = clone.querySelector('.raw-text');
            rawText.textContent = data.transcript || 'Raw text not available in cache.';
            rawText.classList.remove('hidden');
            clone.querySelector('.pdf-container').classList.add('hidden');
        }

        const panels = [saydoPanel, summaryPanel, transcriptPanel];

        // Setup Tabs
        tabs.forEach(tab => {
            tab.addEventListener('click', (e) => {
                // remove active classes
                tabs.forEach(t => t.classList.remove('active'));
                panels.forEach(p => p.classList.add('hidden'));

                // set current active
                e.target.classList.add('active');
                const target = e.target.getAttribute('data-target');
                currentTabType = target;

                if (target === 'saydo') saydoPanel.classList.remove('hidden');
                if (target === 'summary') summaryPanel.classList.remove('hidden');
                if (target === 'transcript') transcriptPanel.classList.remove('hidden');
            });
        });

        // Setup Download
        downloadBtn.addEventListener('click', () => {
            window.open(`/api/download/${companyTicker}/${data.quarter}/${data.year}/${currentTabType}`, '_blank');
        });

        return content;
    }

    // Init
    fetchCompanies();
});
