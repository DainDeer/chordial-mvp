from typing import List

def chunk_message(content: str, max_length: int = 2000) -> List[str]:
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
            sentences = split_into_sentences(paragraph)
            
            for sentence in sentences:
                # if even a sentence is too long, hard split it
                if len(sentence) > max_length:
                    # rare but real: urls or continuous text. every pass
                    # must consume input or flush the chunk - the previous
                    # shape took a zero-length slice once the chunk was
                    # exactly full and looped forever (found by the tether's
                    # attribution test; live it meant one 4097-char
                    # unbroken reply could hang the send path).
                    while len(sentence) > 0:
                        space = max_length - len(current_chunk)
                        if space > 0:
                            current_chunk += sentence[:space]
                            sentence = sentence[space:]
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

def split_into_sentences(text: str) -> List[str]:
    """simple sentence splitter"""
    # this is a basic implementation - you might want something more sophisticated
    import re
    
    # split on common sentence endings but keep the punctuation
    sentences = re.split(r'(?<=[.!?])\s+', text)
    
    # filter out empty strings
    return [s.strip() for s in sentences if s.strip()]