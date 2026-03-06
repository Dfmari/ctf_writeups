# 🚩 CTF Writeups & Security Research

![Web Security](https://img.shields.io/badge/Category-Web_Security-blue)
![DFIR](https://img.shields.io/badge/Category-DFIR-red)
![Workflow](https://img.shields.io/badge/Workflow-Obsidian_%E2%86%92_HTML-purple)

This repository contains my Capture The Flag (CTF) writeups, focusing primarily on **Web Application Security** and **Digital Forensics (DFIR)**. 

Rather than just posting payloads, these writeups document the complete infiltration and analysis process: including recon, dead ends, logic mapping, and custom tool creation.

### 🌐 [View the Live Website Here](https://dfmari.github.io/ctf_writeups/)

---

## 🏆 Current Writeups

| Challenge                   | Event           | Category | Vulnerabilities / Techniques                                                                            |
| :-------------------------- | :-------------- | :------- | :------------------------------------------------------------------------------------------------------ |
| **MSN Revive**              | srdnlenCTF 2026 | Web      | Node.js/Nginx Parser Differential, Path Normalization, IDOR                                             |
| **DoubleShop** (WIP)        | srdnlenCTF 2026 | Web      | LFI, IP Spoofing via Trusted Header, Apache/Tomcat Path Info Bypass (`..;`)                             |
| **Trilogy of Death I** (WIP)| srdnlenCTF 2026 | DFIR     | Legacy Systems (1999 Corel Linux), PerfectScript Macro Analysis, Known Plaintext Attack (Repeating XOR) |
| **Solved Tasks** (WIP)           | BelkaCTF 2025   | DFIR     | ...                                                                                                     |

---

## 📂 Repository Architecture

This repository acts as both the source and the hosting environment for the static site. Writeups are natively authored in Obsidian and deployed via a custom bash pipeline.

```
.
├── 📁 media/                  # Raw CTF assets, target binaries, and disk images
│   └── srdnlenCTF2026/        
├── 📁 writeups/               # Exported HTML pages, CSS/JS, and screenshots
│   └── srdnlenCTF2026/        
├── 📄 index.html              # Repository root hub / Table of Contents
├── 📄 publish.sh              # Custom deployment script (HTML sanitization)
└── 📄 README.md               # You are here
```
