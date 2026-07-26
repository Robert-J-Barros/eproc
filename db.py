"""
Camada de banco de dados (MySQL/MariaDB) para o crawler do e-Proc.

Tabelas:
    empresas         -- partes já visitadas na busca genérica (dedupe)
    processos         -- processos extraídos que bateram nos filtros
    checkpoint         -- ponto de retomada do loop contínuo (para não
                          reprocessar tudo do zero a cada reinício)

Uso típico:
    from db import Database
    db = Database()
    db.criar_schema()
    db.salvar_processo({...})
    checkpoint = db.carregar_checkpoint()
    db.salvar_checkpoint(termo="LTDA", pagina_empresas=3, ...)
"""

import os
from datetime import datetime

from sqlalchemy import (
    create_engine, text, Column, Integer, String, Text, DateTime,
    Numeric, Boolean, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import IntegrityError

Base = declarative_base()


def _mysql_url() -> str:
    host = os.environ.get("MYSQL_HOST", "mysql")
    port = os.environ.get("MYSQL_PORT", "3306")
    user = os.environ.get("MYSQL_USER", "eproc")
    senha = os.environ.get("MYSQL_PASSWORD", "")
    banco = os.environ.get("MYSQL_DATABASE", "eproc")
    return f"mysql+pymysql://{user}:{senha}@{host}:{port}/{banco}?charset=utf8mb4"


class Empresa(Base):
    """Uma parte (pessoa jurídica) encontrada na busca genérica de nome."""
    __tablename__ = "empresas"

    id = Column(Integer, primary_key=True, autoincrement=True)
    id_pessoa = Column(String(64), unique=True, nullable=False, index=True)
    nome = Column(String(255), nullable=False)
    cpf_cnpj = Column(String(32))
    processada = Column(Boolean, default=False, nullable=False)
    total_processos_encontrados = Column(Integer, default=0)
    criado_em = Column(DateTime, default=datetime.utcnow)
    processada_em = Column(DateTime, nullable=True)


class Processo(Base):
    """Um processo individual que bateu nos critérios de filtro."""
    __tablename__ = "processos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    numero_processo = Column(String(64), unique=True, nullable=False, index=True)
    url = Column(Text)
    id_pessoa_autor = Column(String(64), index=True)
    autor = Column(String(255))
    reu = Column(String(255))
    classe_processual = Column(String(255))
    data_autuacao = Column(String(32))   # texto "dd/mm/aaaa" -- normalizar depois se precisar de range query
    valor_causa = Column(Numeric(14, 2))
    oab_numero = Column(String(32))
    oab_uf = Column(String(2))
    dados_brutos = Column(Text)          # JSON cru extraído, para auditoria/reprocessamento
    encontrado_em = Column(DateTime, default=datetime.utcnow)


class Checkpoint(Base):
    """
    Estado do loop contínuo -- só existe UMA linha (id=1), sempre
    sobrescrita. Permite retomar exatamente de onde parou após um
    restart do container.
    """
    __tablename__ = "checkpoint"

    id = Column(Integer, primary_key=True, default=1)
    termo_busca_atual = Column(String(64))
    pagina_empresas_atual = Column(Integer, default=1)
    id_pessoa_empresa_atual = Column(String(64), nullable=True)
    pagina_processos_empresa_atual = Column(Integer, default=1)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Database:
    def __init__(self):
        self.engine = create_engine(_mysql_url(), pool_pre_ping=True, pool_recycle=3600)
        self.Session = sessionmaker(bind=self.engine)

    def criar_schema(self):
        Base.metadata.create_all(self.engine)

    # --- Empresas ---------------------------------------------------

    def empresa_ja_processada(self, id_pessoa: str) -> bool:
        with self.Session() as s:
            row = s.query(Empresa).filter_by(id_pessoa=id_pessoa).first()
            return bool(row and row.processada)

    def registrar_empresa(self, id_pessoa: str, nome: str, cpf_cnpj: str = None):
        with self.Session() as s:
            existente = s.query(Empresa).filter_by(id_pessoa=id_pessoa).first()
            if existente:
                return existente
            empresa = Empresa(id_pessoa=id_pessoa, nome=nome, cpf_cnpj=cpf_cnpj)
            s.add(empresa)
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
            return empresa

    def marcar_empresa_processada(self, id_pessoa: str, total_processos: int):
        with self.Session() as s:
            s.query(Empresa).filter_by(id_pessoa=id_pessoa).update({
                "processada": True,
                "total_processos_encontrados": total_processos,
                "processada_em": datetime.utcnow(),
            })
            s.commit()

    # --- Processos ----------------------------------------------------

    def salvar_processo(self, dados: dict) -> bool:
        """
        Insere um processo se ele ainda não existir (dedupe por
        numero_processo). Retorna True se inseriu, False se já existia.
        """
        with self.Session() as s:
            existente = s.query(Processo).filter_by(
                numero_processo=dados["numero_processo"]
            ).first()
            if existente:
                return False
            s.add(Processo(**dados))
            try:
                s.commit()
                return True
            except IntegrityError:
                s.rollback()
                return False

    # --- Checkpoint ---------------------------------------------------

    def carregar_checkpoint(self) -> dict | None:
        with self.Session() as s:
            row = s.query(Checkpoint).filter_by(id=1).first()
            if not row:
                return None
            return {
                "termo_busca_atual": row.termo_busca_atual,
                "pagina_empresas_atual": row.pagina_empresas_atual,
                "id_pessoa_empresa_atual": row.id_pessoa_empresa_atual,
                "pagina_processos_empresa_atual": row.pagina_processos_empresa_atual,
            }

    def salvar_checkpoint(self, **campos):
        with self.Session() as s:
            row = s.query(Checkpoint).filter_by(id=1).first()
            if not row:
                row = Checkpoint(id=1)
                s.add(row)
            for chave, valor in campos.items():
                setattr(row, chave, valor)
            s.commit()

    def limpar_checkpoint(self):
        with self.Session() as s:
            s.query(Checkpoint).filter_by(id=1).delete()
            s.commit()
