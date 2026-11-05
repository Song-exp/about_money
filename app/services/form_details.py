"""같은 native 문항 셀의 명시적 작성 요청을 원문 범위의 수집 질문으로 나눈다."""
import hashlib
import re

from app.services.document_layout import compact
from app.services.form_semantics import block_source

REQUEST = re.compile(r"(?:설명해\s*주세요|적어\s*주세요|작성해\s*주세요|기재해\s*주세요|작성해\s*주시기\s*바랍니다)\s*[.!]?$")


def enrich_details(field, block, blocks, source):
    if field["field_type"] != "essay" or block.get("method") != "native" or not block.get("cell_location"):
        return
    index = blocks.index(block)
    local = []
    for following in blocks[index + 1:]:
        if following.get("cell_location") != block["cell_location"] or following.get("method") != "native":
            break
        if re.match(r"^\s*\d+[.)]\s*", following["text"]):
            break
        if following["text"].lstrip().startswith(("※", "예:", "예시:", "*")):
            continue
        local.append(following)
    details = []
    guides = ([block] if any(REQUEST.search(s[0]) for b in local for s in re.finditer(r"[^.!?\n]+[.!?]?", b["text"])) else []) + local
    for guide in guides:
        for sentence in re.finditer(r"[^.!?\n]+[.!?]?", guide["text"]):
            match = REQUEST.search(sentence[0])
            if not match:
                continue
            body = sentence[0][:match.start()]
            if any(t in compact(body) for t in ("작성하지", "예를들어", "선발에영향", "개인정보수집", "글씨크기", "줄간격")):
                continue
            # Keep question clauses ('~는지') intact. Split noun lists only at
            # explicit commas/conjunctions; never interpret alternatives as all required.
            separator = r",\s*" if "는지" in body else r",\s*|(?<=[가-힣])(?:과|와)\s+|\s+(?:및|그리고)\s+"
            boundaries = [0, *[m.end() for m in re.finditer(separator, body)], len(body)]
            ends = [*[m.start() for m in re.finditer(separator, body)], len(body)]
            for start, end in zip(boundaries, ends):
                fragment = body[start:end]
                # Drop the final instruction adverb/particle but preserve exact source.
                tail = re.search(r"(?:도\s+함께|\s+구체적으로|\s+필요한\s+사항에\s+대해|[을를])\s*$", fragment)
                if tail:
                    fragment = fragment[:tail.start()]
                fragment = fragment.strip()
                if len(compact(fragment)) < 3:
                    continue
                offset = guide["start"] + sentence.start() + start + body[start:end].find(fragment)
                proof = {**block_source(guide, source), "text_start": offset, "text_end": offset + len(fragment), "evidence": fragment}
                identity = hashlib.sha256(f"{guide['location']}:{offset - guide['start']}:{compact(fragment)}".encode()).hexdigest()[:12]
                details.append({"kind": "prompted_detail", "key": "original_detail_" + identity,
                                "evidence": fragment, "context": sentence[0].strip(), "source": proof,
                                "context_source": {**block_source(guide, source), "text_start": guide["start"] + sentence.start(),
                                                   "text_end": guide["start"] + sentence.end(), "evidence": sentence[0]}})
    if details:
        field["content_requirements"] = [*field.get("content_requirements", []), *details]
        proofs = list(field.get("instruction_sources", []))
        if not proofs and field.get("instruction_source"):
            proofs.append(field["instruction_source"])
        for guide in guides:
            if not any(s.get("text_start") == guide["start"] and s.get("text_end") == guide["end"] for s in proofs):
                proofs.append(block_source(guide, source))
        proofs.sort(key=lambda s: s["text_start"])
        field.update(instruction_sources=proofs, instruction_source=proofs[0],
                     instructions="\n".join(s["evidence"] for s in proofs), instruction_evidence="\n".join(s["evidence"] for s in proofs))
