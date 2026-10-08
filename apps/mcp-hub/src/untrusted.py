import re

def wrap_untrusted(text: str) -> str:
    """Wraps externally-sourced text to mitigate prompt injection.
    
    Delimits the content inside an inert fence and neutralizes escape sequences
    and instruction-shaped patterns.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
        
    # 1. Neutralize fence escape sequences
    neutralized = text
    neutralized = re.sub(r'(?i)===?\s*UNTRUSTED\s*CONTENT\s*START\s*===?', '[start]', neutralized)
    neutralized = re.sub(r'(?i)===?\s*UNTRUSTED\s*CONTENT\s*END\s*===?', '[end]', neutralized)
    
    # 2. Strip/escape instruction-shaped patterns
    instruction_patterns = [
        (r'(?i)\bignore\s+(?:previous|above|all)\s+instructions\b', '[override]'),
        (r'(?i)\bsystem\s+override\b', '[sys-override]'),
        (r'(?i)\binstruction\s+override\b', '[inst-override]'),
        (r'(?i)\byou\s+are\s+a\b', 'you_are_registered_as'),
        (r'(?i)\bnew\s+instruction\b', 'new_info'),
        (r'(?i)\bdeveloper\s+mode\b', 'user_mode'),
    ]
    for pattern, replacement in instruction_patterns:
        neutralized = re.sub(pattern, replacement, neutralized)
        
    return f"\n=== UNTRUSTED CONTENT START ===\n{neutralized}\n=== UNTRUSTED CONTENT END ===\n"

def wrap_untrusted_short(text: str) -> str:
    """Neutralizes instruction-shaped patterns in short text (verbatim fields) without fences or marker strings.
    
    Prevents fence leakage in user-facing echoed outputs.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
        
    neutralized = text
    # Replace any fence sequences to prevent them from injecting markers
    neutralized = re.sub(r'(?i)===?\s*UNTRUSTED\s*CONTENT\s*START\s*===?', '[start]', neutralized)
    neutralized = re.sub(r'(?i)===?\s*UNTRUSTED\s*CONTENT\s*END\s*===?', '[end]', neutralized)
    
    instruction_patterns = [
        (r'(?i)\bignore\s+(?:previous|above|all)\s+instructions\b', '[override]'),
        (r'(?i)\bsystem\s+override\b', '[sys-override]'),
        (r'(?i)\binstruction\s+override\b', '[inst-override]'),
        (r'(?i)\byou\s+are\s+a\b', 'you_are_registered_as'),
        (r'(?i)\bnew\s+instruction\b', 'new_info'),
        (r'(?i)\bdeveloper\s+mode\b', 'user_mode'),
    ]
    for pattern, replacement in instruction_patterns:
        neutralized = re.sub(pattern, replacement, neutralized)
        
    return neutralized
