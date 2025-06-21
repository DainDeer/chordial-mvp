from typing import List

def chunk_message(self, content: str, max_length: int = 2000) -> List[str]:
    """intelligently chunk a message into discord-sized pieces"""
    if len(content) <= max_length:
        return [content]
    
    chunks = []
    current_chunk = ""
    
    # first try to split by paragraphs (double newlines)
    paragraphs = content.split('\n\n')
    
    for paragraph in paragraphs:
        # if a single paragraph is too long, we need to split it further
        if len(paragraph) > max_length:
            # split by sentences
            sentences = self._split_into_sentences(paragraph)
            
            for sentence in sentences:
                # if even a sentence is too long, hard split it
                if len(sentence) > max_length:
                    # this is rare but could happen with urls or continuous text
                    while len(sentence) > 0:
                        if len(current_chunk) + len(sentence[:max_length - len(current_chunk)]) <= max_length:
                            split_point = max_length - len(current_chunk)
                            current_chunk += sentence[:split_point]
                            sentence = sentence[split_point:]
                        else:
                            chunks.append(current_chunk.strip())
                            current_chunk = ""
                else:
                    # normal sentence processing
                    if len(current_chunk) + len(sentence) + 1 <= max_length:
                        if current_chunk:
                            current_chunk += " " + sentence
                        else:
                            current_chunk = sentence
                    else:
                        chunks.append(current_chunk.strip())
                        current_chunk = sentence
        else:
            # paragraph fits
            if len(current_chunk) + len(paragraph) + 2 <= max_length:
                if current_chunk:
                    current_chunk += "\n\n" + paragraph
                else:
                    current_chunk = paragraph
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = paragraph
    
    # don't forget the last chunk
    if current_chunk:
        chunks.append(current_chunk.strip())
    
    return chunks

def split_into_sentences(self, text: str) -> List[str]:
    """simple sentence splitter"""
    # this is a basic implementation - you might want something more sophisticated
    import re
    
    # split on common sentence endings but keep the punctuation
    sentences = re.split(r'(?<=[.!?])\s+', text)
    
    # filter out empty strings
    return [s.strip() for s in sentences if s.strip()]