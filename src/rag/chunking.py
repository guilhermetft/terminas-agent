"""Divisão de texto em blocos com sobreposição, preservando números de linha para citação."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_CHUNK_SIZE = 800  # caracteres
DEFAULT_OVERLAP = 100


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    start_line: int  # 1-based, inclusivo
    end_line: int

    @property
    def lines(self) -> str:
        """Faixa de linhas no formato do contrato A2A: '10-25' ou '7'."""
        if self.start_line == self.end_line:
            return str(self.start_line)
        return f"{self.start_line}-{self.end_line}"


def _units(text: str, chunk_size: int) -> list[tuple[int, str]]:
    """(nº da linha, texto) por linha; linhas maiores que o bloco são fatiadas."""
    units: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if len(line) <= chunk_size:
            units.append((number, line))
        else:
            for start in range(0, len(line), chunk_size):
                units.append((number, line[start : start + chunk_size]))
    return units


def chunk_text(
    text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_OVERLAP
) -> list[Chunk]:
    """Fatia o texto em blocos de até `chunk_size` caracteres.

    - Acumula linhas inteiras; ao estourar, prefere cortar na última linha em branco da
      segunda metade do bloco (fronteira de parágrafo), senão corta na linha.
    - O bloco seguinte recua linhas até `overlap` caracteres para manter contexto.
    - Linhas maiores que `chunk_size` são fatiadas e mantêm o mesmo número de linha.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size deve ser >= 1.")
    if not 0 <= overlap < chunk_size:
        raise ValueError("overlap deve estar entre 0 e chunk_size - 1.")

    units = _units(text, chunk_size)
    chunks: list[Chunk] = []
    start = 0
    while start < len(units):
        end, size = start, 0
        while end < len(units) and (end == start or size + len(units[end][1]) + 1 <= chunk_size):
            size += len(units[end][1]) + 1
            end += 1

        if end < len(units):  # bloco foi cortado: tenta fronteira de parágrafo
            for k in range(end - 1, start + (end - start) // 2, -1):
                if not units[k][1].strip():
                    end = k + 1
                    break

        filled = [i for i in range(start, end) if units[i][1].strip()]
        if filled:  # descarta linhas em branco nas pontas (linhas citadas = linhas com texto)
            first, last = filled[0], filled[-1]
            body = "\n".join(u[1] for u in units[first : last + 1])
            chunks.append(Chunk(len(chunks), body, units[first][0], units[last][0]))

        if end >= len(units):
            break
        back, kept = end, 0
        while back > start + 1 and kept + len(units[back - 1][1]) + 1 <= overlap:
            back -= 1
            kept += len(units[back][1]) + 1
        start = back
    return chunks
