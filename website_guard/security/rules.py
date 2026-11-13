from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from .risk import RiskEngine

CONTROL = {"administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks"}
ADMIN_TERMS = {"admin", "administracao", "staff", "moderacao", "moderação", "seguranca", "segurança", "logs", "auditoria", "backups", "sistema", "interno"}


@dataclass(frozen=True)
class RuleFinding:
    rule_id: str
    category: str
    kind: str
    severity: str
    title: str
    description: str
    evidence: str
    resource_type: str
    resource_id: Any
    resource_name: str
    reason: str
    recommendation: str


class SecurityRuleEngine:
    """Registro extensível de regras determinísticas, sem decisões por IA."""

    def __init__(self) -> None:
        self._rules = (
            self._roles,
            self._members,
            self._channels,
            self._automod,
            self._bots,
            self._webhooks,
            self._integrations,
            self._guild,
        )

    def analyze(
        self,
        snapshot: dict[str, Any],
        *,
        admin_role_ids: Iterable[int] = (),
        bella_user_id: int = 0,
        enabled_areas: set[str] | None = None,
    ) -> list[RuleFinding]:
        context = {
            "admin_role_ids": {int(item) for item in admin_role_ids},
            "bella_user_id": int(bella_user_id),
            "enabled_areas": enabled_areas or {"guild", "roles", "members", "channels", "automod", "bots", "webhooks", "integrations"},
        }
        findings = [finding for rule in self._rules for finding in rule(snapshot, context)]
        rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
        return sorted(findings, key=lambda item: (rank.get(item.severity, 9), item.category, item.title))

    def _roles(self, snapshot: dict[str, Any], context: dict[str, Any]) -> list[RuleFinding]:
        if "roles" not in context["enabled_areas"]:
            return []
        guild_id = int(snapshot.get("guild", {}).get("id", 0) or 0)
        findings: list[RuleFinding] = []
        roles = [role for role in snapshot.get("roles", []) if isinstance(role, dict)]
        highest_position = max((int(role.get("position", 0) or 0) for role in roles), default=0)
        for role in roles:
            if not isinstance(role, dict):
                continue
            permissions = set(role.get("sensitive_permissions", []))
            if not permissions:
                continue
            role_id = int(role.get("id", 0) or 0)
            members = int(role.get("member_count", 0) or 0)
            authorized = role_id in context["admin_role_ids"]
            if "administrator" in permissions:
                findings.append(self._finding(
                    "role-administrator", "roles", "FINDING",
                    RiskEngine.role_administrator(is_everyone=role_id == guild_id, managed=bool(role.get("managed")), authorized=authorized, member_count=members),
                    "Cargo com permissão Administrator",
                    "Administrator concede controle amplo e ignora overwrites específicos de canais.",
                    f"Cargo: {role.get('name')} | membros afetados: {members} | gerenciado: {bool(role.get('managed'))}",
                    "role", role_id, str(role.get("name", "cargo")),
                    "A severidade considera alcance, RBAC esperado, quantidade de membros e se o cargo é gerenciado.",
                    "Confirme a necessidade de Administrator e prefira permissões específicas.",
                ))
                continue
            control = sorted(permissions & CONTROL)
            if len(control) >= 2:
                findings.append(self._finding(
                    "role-control-plane-combination", "roles", "WARNING",
                    RiskEngine.control_plane_combination(member_count=members, authorized=authorized, permission_count=len(control)),
                    "Combinação de permissões administrativas",
                    "A combinação pode alterar estrutura, cargos, integrações ou webhooks sem Administrator.",
                    f"Cargo: {role.get('name')} | permissões: {', '.join(control)} | membros: {members}",
                    "role", role_id, str(role.get("name", "cargo")),
                    "Combinações de controle podem produzir escalonamento indireto.",
                    "Separe responsabilidades e aplique o princípio do menor privilégio.",
                ))
            if (
                "manage_roles" in permissions
                and not authorized
                and members > 0
                and highest_position > 0
                and int(role.get("position", 0) or 0) >= max(1, int(highest_position * 0.8))
            ):
                findings.append(self._finding(
                    "role-manage-roles-high-hierarchy", "roles", "WARNING", "HIGH",
                    "Cargo com Manage Roles em posição hierárquica alta",
                    "O cargo pode gerenciar cargos abaixo dele e está próximo do topo da hierarquia.",
                    f"Cargo: {role.get('name')} | posição: {role.get('position')} de {highest_position} | membros: {members}",
                    "role", role_id, str(role.get("name", "cargo")),
                    "A combinação entre Manage Roles, posição e quantidade de membros define o alcance real.",
                    "Revise a posição do cargo e mantenha acima dele todos os cargos que ele não deve gerenciar.",
                ))
            if "mention_everyone" in permissions and members >= 10 and not authorized:
                findings.append(self._finding(
                    "role-mention-everyone", "roles", "WARNING", "MEDIUM",
                    "Cargo amplo pode mencionar everyone/here", "Menções globais podem causar spam e engenharia social.",
                    f"Cargo: {role.get('name')} | membros afetados: {members}", "role", role_id, str(role.get("name", "cargo")),
                    "O risco cresce com a quantidade de membros que pode usar a permissão.", "Restrinja Mention Everyone aos cargos necessários.",
                ))
        return findings

    def _members(self, snapshot: dict[str, Any], context: dict[str, Any]) -> list[RuleFinding]:
        if "members" not in context["enabled_areas"]:
            return []
        role_map = {int(role.get("id", 0)): role for role in snapshot.get("roles", []) if isinstance(role, dict)}
        findings: list[RuleFinding] = []
        for member in snapshot.get("members", []):
            if not isinstance(member, dict) or member.get("owner") or member.get("bot"):
                continue
            permissions = set(member.get("sensitive_permissions", []))
            role_ids = [int(item) for item in member.get("role_ids", [])]
            privileged_roles = [role_map[item].get("name", str(item)) for item in role_ids if item in role_map and set(role_map[item].get("sensitive_permissions", []))]
            if "administrator" in permissions and not set(role_ids) & context["admin_role_ids"]:
                findings.append(self._finding(
                    "member-effective-administrator", "members", "FINDING", "HIGH",
                    "Membro com privilégio administrativo efetivo",
                    "A soma dos cargos do membro resulta em acesso administrativo amplo.",
                    f"Membro: {member.get('name')} | cargos relevantes: {', '.join(map(str, privileged_roles))}",
                    "member", member.get("id", 0), str(member.get("name", "membro")),
                    "O cálculo usa as permissões efetivas do Discord e não classifica comportamento.",
                    "Valide se o conjunto de cargos corresponde à função administrativa esperada.",
                ))
        return findings

    def _channels(self, snapshot: dict[str, Any], context: dict[str, Any]) -> list[RuleFinding]:
        if "channels" not in context["enabled_areas"]:
            return []
        role_members = {int(item.get("id", 0)): int(item.get("member_count", 0)) for item in snapshot.get("roles", []) if isinstance(item, dict)}
        guild_id = int(snapshot.get("guild", {}).get("id", 0) or 0)
        findings: list[RuleFinding] = []
        for channel in snapshot.get("channels", []):
            if not isinstance(channel, dict):
                continue
            channel_id = int(channel.get("id", 0) or 0)
            name = str(channel.get("name", "canal"))
            normalized = name.casefold().replace("-", " ").replace("_", " ")
            if any(term in normalized for term in ADMIN_TERMS) and channel.get("public_view"):
                findings.append(self._finding(
                    "channel-admin-public", "channels", "FINDING", "HIGH",
                    "Canal administrativo potencialmente público", "O nome sugere conteúdo administrativo, mas @everyone consegue visualizar o canal.",
                    f"Canal: #{name} | tipo: {channel.get('type')}", "channel", channel_id, name,
                    "Logs, backups e auditorias podem conter informações operacionais sensíveis.",
                    "Revise o overwrite de @everyone e conceda acesso apenas aos cargos necessários.",
                ))
            if channel.get("category_id") and not channel.get("permissions_synced", True):
                findings.append(self._finding(
                    "channel-unsynced-permissions", "channels", "WARNING", "LOW",
                    "Permissões divergentes da categoria", "O canal possui overwrites próprios diferentes da categoria.",
                    f"Canal: #{name} | categoria: {channel.get('category_id')}", "channel", channel_id, name,
                    "Pode ser intencional, mas também manter acessos antigos.", "Compare com a categoria e sincronize se a exceção não for necessária.",
                ))
            for overwrite in channel.get("overwrites", []):
                if not isinstance(overwrite, dict):
                    continue
                sensitive = set(overwrite.get("effective_sensitive_permissions", overwrite.get("sensitive_allow", [])))
                if not sensitive:
                    continue
                target_type = str(overwrite.get("target_type", "unknown"))
                target_id = int(overwrite.get("target_id", 0) or 0)
                severity = "HIGH" if sensitive & CONTROL else "MEDIUM"
                if target_type == "role" and role_members.get(target_id, 1) <= 2 and severity == "HIGH":
                    severity = "MEDIUM"
                if target_type == "role" and target_id == guild_id and sensitive & CONTROL:
                    severity = "CRITICAL"
                findings.append(self._finding(
                    "channel-sensitive-overwrite", "channels", "WARNING", severity,
                    "Overwrite concede permissão sensível", "Um overwrite individual ou de cargo libera ações administrativas neste canal.",
                    f"Canal: #{name} | alvo: {target_type}:{target_id} | permissões: {', '.join(sorted(sensitive))}",
                    "channel", channel_id, name,
                    "Overwrites individuais são fáceis de esquecer e podem ampliar o impacto de uma conta comprometida.",
                    "Confirme o alvo e prefira cargos bem definidos em vez de usuários individuais.",
                ))
        return findings

    def _automod(self, snapshot: dict[str, Any], context: dict[str, Any]) -> list[RuleFinding]:
        if "automod" not in context["enabled_areas"]:
            return []
        guild = snapshot.get("guild", {})
        if "automod" in set(snapshot.get("areas_unavailable", [])):
            return [self._finding("automod-unavailable", "automod", "RECOMMENDATION", "INFO", "AutoMod não pôde ser analisado", "A API oficial não retornou as regras.", "Área não analisada por permissão ou erro da API.", "guild", guild.get("id", 0), str(guild.get("name", "servidor")), "Sem leitura confiável não há conclusão de segurança.", "Conceda Gerenciar Servidor somente se desejar essa auditoria.")]
        rules = [item for item in snapshot.get("automod", []) if isinstance(item, dict)]
        if not rules:
            return [self._finding("automod-no-rules", "automod", "RECOMMENDATION", "INFO", "Avaliar proteção nativa de AutoMod", "Nenhuma regra nativa foi encontrada; isso não confirma vulnerabilidade.", "Regras encontradas: 0", "guild", guild.get("id", 0), str(guild.get("name", "servidor")), "A moderação pode estar coberta por outras ferramentas ou fluxo manual.", "Avalie regras suportadas pelo Discord para spam, menções em massa e palavras-chave.")]
        findings: list[RuleFinding] = []
        for rule in rules:
            if not rule.get("enabled", False):
                findings.append(self._finding("automod-disabled-rule", "automod", "WARNING", "LOW", "Regra de AutoMod desativada", "A regra existe, mas não aplica ações.", f"Regra: {rule.get('name')} | trigger: {rule.get('trigger_type')}", "automod_rule", rule.get("id", 0), str(rule.get("name", "regra")), "A pausa pode ser intencional.", "Confirme se a desativação ainda é necessária."))
            exemptions = len(rule.get("exempt_role_ids", [])) + len(rule.get("exempt_channel_ids", []))
            if exemptions >= 10:
                findings.append(self._finding("automod-many-exemptions", "automod", "WARNING", "MEDIUM", "AutoMod com muitas exceções", "Muitas exceções podem reduzir a cobertura real.", f"Regra: {rule.get('name')} | exceções: {exemptions}", "automod_rule", rule.get("id", 0), str(rule.get("name", "regra")), "A severidade considera quantidade e não presume erro.", "Revise cargos e canais liberados e remova exceções antigas."))
        return findings

    def _bots(self, snapshot: dict[str, Any], context: dict[str, Any]) -> list[RuleFinding]:
        if "bots" not in context["enabled_areas"]:
            return []
        findings: list[RuleFinding] = []
        for bot in snapshot.get("bots", []):
            if not isinstance(bot, dict):
                continue
            permissions = set(bot.get("sensitive_permissions", []))
            broad = "administrator" in permissions or len(permissions & CONTROL) >= 2
            if not broad:
                continue
            bot_id = int(bot.get("id", 0) or 0)
            findings.append(self._finding(
                "bot-broad-privileges", "bots", "WARNING", RiskEngine.bot_privileges(permissions, is_bella=bot_id == context["bella_user_id"]),
                "Bot com privilégios administrativos amplos", "O bot possui permissões com grande alcance caso a aplicação ou token seja comprometido.",
                f"Bot: {bot.get('name')} | permissões: {', '.join(sorted(permissions))} | cargos: {', '.join(bot.get('role_names', []))}",
                "bot", bot_id, str(bot.get("name", "bot")), "O scanner não presume que o bot seja malicioso.",
                "Verifique se todas as permissões são necessárias e mantenha o cargo abaixo dos cargos humanos críticos.",
            ))
        return findings

    def _webhooks(self, snapshot: dict[str, Any], context: dict[str, Any]) -> list[RuleFinding]:
        if "webhooks" not in context["enabled_areas"]:
            return []
        findings: list[RuleFinding] = []
        by_channel = Counter(int(item.get("channel_id", 0) or 0) for item in snapshot.get("webhooks", []) if isinstance(item, dict))
        for webhook in snapshot.get("webhooks", []):
            if isinstance(webhook, dict) and webhook.get("user_id") is None:
                findings.append(self._finding("webhook-unowned", "webhooks", "WARNING", "LOW", "Webhook sem criador disponível", "A API não retornou um usuário criador.", f"Webhook: {webhook.get('name')} | canal: {webhook.get('channel_id')} | tipo: {webhook.get('type')}", "webhook", webhook.get("id", 0), str(webhook.get("name", "webhook")), "É comum em webhooks de aplicação, mas merece inventário.", "Confirme se pertence a uma integração ativa. Tokens nunca são coletados."))
        for channel_id, count in by_channel.items():
            if channel_id and count >= 6:
                findings.append(self._finding("webhook-channel-volume", "webhooks", "WARNING", "MEDIUM", "Muitos webhooks no mesmo canal", "A concentração aumenta a superfície de publicação externa.", f"Canal: {channel_id} | webhooks: {count}", "channel", channel_id, str(channel_id), "O volume não prova abuso.", "Remova webhooks antigos e documente o responsável."))
        return findings

    def _integrations(self, snapshot: dict[str, Any], context: dict[str, Any]) -> list[RuleFinding]:
        if "integrations" not in context["enabled_areas"]:
            return []
        role_map = {int(item.get("id", 0)): item for item in snapshot.get("roles", []) if isinstance(item, dict)}
        findings: list[RuleFinding] = []
        for integration in snapshot.get("integrations", []):
            if not isinstance(integration, dict):
                continue
            role = role_map.get(int(integration.get("role_id", 0) or 0))
            if role and "administrator" in set(role.get("sensitive_permissions", [])):
                findings.append(self._finding("integration-admin-role", "integrations", "WARNING", "MEDIUM", "Integração vinculada a cargo Administrator", "A integração possui cargo gerenciado com controle amplo.", f"Integração: {integration.get('name')} | cargo: {role.get('name')}", "integration", integration.get("id", 0), str(integration.get("name", "integração")), "Pode ser esperado, mas amplia impacto de comprometimento.", "Revise as permissões concedidas durante a instalação."))
        return findings

    def _guild(self, snapshot: dict[str, Any], context: dict[str, Any]) -> list[RuleFinding]:
        if "guild" not in context["enabled_areas"]:
            return []
        guild = snapshot.get("guild", {})
        findings: list[RuleFinding] = []
        if int(guild.get("verification_level", 0) or 0) <= 1:
            findings.append(self._finding("guild-low-verification", "guild", "RECOMMENDATION", "LOW", "Revisar nível de verificação", "O nível de verificação do servidor é baixo.", f"Nível atual: {guild.get('verification_level', 0)}", "guild", guild.get("id", 0), str(guild.get("name", "servidor")), "Servidores públicos podem se beneficiar de barreiras adicionais; servidores privados podem não precisar.", "Avalie aumentar o nível de verificação conforme o perfil da comunidade."))
        if int(guild.get("mfa_level", 0) or 0) == 0:
            findings.append(self._finding("guild-admin-mfa-disabled", "guild", "RECOMMENDATION", "MEDIUM", "2FA administrativa não exigida", "O servidor não exige 2FA para ações administrativas elegíveis.", "MFA level: 0", "guild", guild.get("id", 0), str(guild.get("name", "servidor")), "A exigência reduz risco de contas administrativas comprometidas.", "Avalie habilitar a exigência de 2FA para moderação nas configurações do servidor."))
        return findings

    @staticmethod
    def _finding(*args: Any) -> RuleFinding:
        return RuleFinding(*args)
