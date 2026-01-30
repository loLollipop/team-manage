"""
Team 管理服务
用于管理 Team 账号的导入、同步、成员管理等功能
"""
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Team, TeamAccount
from app.services.chatgpt import ChatGPTService
from app.services.encryption import encryption_service
from app.utils.token_parser import TokenParser
from app.utils.jwt_parser import JWTParser
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


class TeamService:
    """Team 管理服务类"""

    def __init__(self):
        """初始化 Team 管理服务"""
        from app.services.chatgpt import chatgpt_service
        self.chatgpt_service = chatgpt_service
        self.token_parser = TokenParser()
        self.jwt_parser = JWTParser()

    async def _handle_api_error(self, result: Dict[str, Any], team: Team, db_session: AsyncSession) -> bool:
        """
        检查结果是否表示账号被封禁或 Token 失效,如果是则更新状态
        
        Returns:
            bool: 是否已处理致命错误
        """
        error_code = result.get("error_code")
        if error_code == "account_deactivated":
            logger.warning(f"检测到账号封禁 (account_deactivated),更新 Team {team.id} 状态为 banned")
            team.status = "banned"
            await db_session.commit()
            return True
        
        if error_code == "token_invalidated":
            logger.warning(f"检测到 Token 失效 (token_invalidated),更新 Team {team.id} 状态为 error")
            team.status = "error"
            await db_session.commit()
            return True
            
        return False

    async def ensure_access_token(self, team: Team, db_session: AsyncSession) -> Optional[str]:
        """
        确保 AT Token 有效,如果过期则尝试刷新
        
        Args:
            team: Team 对象
            db_session: 数据库会话
            
        Returns:
            有效的 AT Token, 刷新失败返回 None
        """
        try:
            # 1. 解密当前 Token
            access_token = encryption_service.decrypt_token(team.access_token_encrypted)
            
            # 2. 检查是否过期
            if not self.jwt_parser.is_token_expired(access_token):
                return access_token
                
            logger.info(f"Team {team.id} ({team.email}) Token 已过期, 尝试刷新")
        except Exception as e:
            logger.error(f"解密或验证 Token 失败: {e}")
            access_token = None # 可能是解密失败，强制走刷新流程

        # 3. 尝试使用 session_token 刷新
        if team.session_token_encrypted:
            session_token = encryption_service.decrypt_token(team.session_token_encrypted)
            refresh_result = await self.chatgpt_service.refresh_access_token_with_session_token(
                session_token, db_session
            )
            if refresh_result["success"]:
                new_at = refresh_result["access_token"]
                logger.info(f"Team {team.id} 通过 session_token 成功刷新 AT")
                team.access_token_encrypted = encryption_service.encrypt_token(new_at)
                await db_session.commit()
                return new_at

        # 4. 尝试使用 refresh_token 刷新
        if team.refresh_token_encrypted and team.client_id:
            refresh_token = encryption_service.decrypt_token(team.refresh_token_encrypted)
            refresh_result = await self.chatgpt_service.refresh_access_token_with_refresh_token(
                refresh_token, team.client_id, db_session
            )
            if refresh_result["success"]:
                new_at = refresh_result["access_token"]
                new_rt = refresh_result.get("refresh_token")
                logger.info(f"Team {team.id} 通过 refresh_token 成功刷新 AT")
                team.access_token_encrypted = encryption_service.encrypt_token(new_at)
                if new_rt:
                    team.refresh_token_encrypted = encryption_service.encrypt_token(new_rt)
                await db_session.commit()
                return new_at
        
        logger.warning(f"Team {team.id} Token 已过期且刷新失败")
        team.status = "error"
        await db_session.commit()
        return None

    async def import_team_single(
        self,
        access_token: Optional[str],
        db_session: AsyncSession,
        email: Optional[str] = None,
        account_id: Optional[str] = None,
        refresh_token: Optional[str] = None,
        session_token: Optional[str] = None,
        client_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        单个导入 Team

        Args:
            access_token: AT Token (可选,如果提供 RT/ST 可自动获取)
            db_session: 数据库会话
            email: 邮箱 (可选,如果不提供则从 Token 中提取)
            account_id: Account ID (可选,如果不提供则从 API 获取并导入所有活跃的)

        Returns:
            结果字典,包含 success, team_id (第一个导入的), message, error
        """
        try:
            # 1. 检查并尝试刷新 Token (如果 AT 缺失或过期)
            is_at_valid = False
            if access_token:
                try:
                    if not self.jwt_parser.is_token_expired(access_token):
                        is_at_valid = True
                except:
                    pass
            
            if not is_at_valid:
                logger.info("导入时 AT 缺失或过期, 尝试使用 ST/RT 刷新")
                # 尝试 session_token
                if session_token:
                    refresh_result = await self.chatgpt_service.refresh_access_token_with_session_token(
                        session_token, db_session
                    )
                    if refresh_result["success"]:
                        access_token = refresh_result["access_token"]
                        is_at_valid = True
                        logger.info("导入时通过 session_token 成功获取 AT")
                
                # 尝试 refresh_token
                if not is_at_valid and refresh_token and client_id:
                    refresh_result = await self.chatgpt_service.refresh_access_token_with_refresh_token(
                        refresh_token, client_id, db_session
                    )
                    if refresh_result["success"]:
                        access_token = refresh_result["access_token"]
                        # RT 刷新可能会返回新的 RT
                        if refresh_result.get("refresh_token"):
                            refresh_token = refresh_result["refresh_token"]
                        is_at_valid = True
                        logger.info("导入时通过 refresh_token 成功获取 AT")

            if not access_token or not is_at_valid:
                return {
                    "success": False,
                    "team_id": None,
                    "message": None,
                    "error": "缺少有效的 Access Token，且无法通过 Session/Refresh Token 刷新"
                }

            # 2. 如果没有提供邮箱,从 Token 中提取
            if not email:
                email = self.jwt_parser.extract_email(access_token)
                if not email:
                    return {
                        "success": False,
                        "team_id": None,
                        "message": None,
                        "error": "无法从 Token 中提取邮箱,请手动提供邮箱"
                    }

            # 2. 调用 ChatGPT API 获取账户信息
            account_result = await self.chatgpt_service.get_account_info(
                access_token,
                db_session
            )

            if not account_result["success"]:
                return {
                    "success": False,
                    "team_id": None,
                    "message": None,
                    "error": f"获取账户信息失败: {account_result['error']}"
                }

            team_accounts = account_result["accounts"]

            if not team_accounts:
                return {
                    "success": False,
                    "team_id": None,
                    "message": None,
                    "error": "该 Token 没有关联任何 Team 账户"
                }

            # 3. 确定要导入的账户列表
            accounts_to_import = []

            if account_id:
                # 如果用户指定了 account_id, 查找对应的账户
                for acc in team_accounts:
                    if acc["account_id"] == account_id:
                        accounts_to_import.append(acc)
                        break

                if not accounts_to_import:
                    return {
                        "success": False,
                        "team_id": None,
                        "message": None,
                        "error": f"指定的 account_id {account_id} 不存在"
                    }
            else:
                # 没指定 ID，导入所有活跃的 Team
                for acc in team_accounts:
                    if acc["has_active_subscription"]:
                        accounts_to_import.append(acc)
                
                # 如果一个活跃的都没找到，保底使用第一个
                if not accounts_to_import:
                    accounts_to_import.append(team_accounts[0])

            # 4. 循环处理这些账户
            imported_ids = []
            skipped_ids = []
            
            for selected_account in accounts_to_import:
                # 检查是否已存在 (根据 account_id)
                stmt = select(Team).where(
                    Team.account_id == selected_account["account_id"]
                )
                result = await db_session.execute(stmt)
                existing_team = result.scalar_one_or_none()

                if existing_team:
                    skipped_ids.append(selected_account["account_id"])
                    continue

                # 获取成员列表
                members_result = await self.chatgpt_service.get_members(
                    access_token,
                    selected_account["account_id"],
                    db_session
                )

                current_members = 0
                if members_result["success"]:
                    current_members = members_result["total"]

                # 解析过期时间
                expires_at = None
                if selected_account["expires_at"]:
                    try:
                        # ISO 8601 格式: 2026-02-21T23:10:05+00:00
                        expires_at = datetime.fromisoformat(
                            selected_account["expires_at"].replace("+00:00", "")
                        )
                    except Exception as e:
                        logger.warning(f"解析过期时间失败: {e}")

                # 确定状态
                status = "active"
                if current_members >= 6:
                    status = "full"
                elif expires_at and expires_at < datetime.now():
                    status = "expired"

                # 加密 AT Token
                encrypted_token = encryption_service.encrypt_token(access_token)
                encrypted_rt = encryption_service.encrypt_token(refresh_token) if refresh_token else None
                encrypted_st = encryption_service.encrypt_token(session_token) if session_token else None

                # 创建 Team 记录
                team = Team(
                    email=email,
                    access_token_encrypted=encrypted_token,
                    refresh_token_encrypted=encrypted_rt,
                    session_token_encrypted=encrypted_st,
                    client_id=client_id,
                    encryption_key_id="default",
                    account_id=selected_account["account_id"],
                    team_name=selected_account["name"],
                    plan_type=selected_account["plan_type"],
                    subscription_plan=selected_account["subscription_plan"],
                    expires_at=expires_at,
                    current_members=current_members,
                    max_members=6,
                    status=status,
                    last_sync=get_now()
                )

                db_session.add(team)
                await db_session.flush()  # 获取 team.id

                # 创建 TeamAccount 记录 (保存所有 Team 账户)
                for acc in team_accounts:
                    team_account = TeamAccount(
                        team_id=team.id,
                        account_id=acc["account_id"],
                        account_name=acc["name"],
                        is_primary=(acc["account_id"] == selected_account["account_id"])
                    )
                    db_session.add(team_account)
                
                imported_ids.append(team.id)

            # 5. 返回结果总结
            if not imported_ids and skipped_ids:
                return {
                    "success": False,
                    "team_id": None,
                    "message": None,
                    "error": f"共发现 {len(skipped_ids)} 个 Team 账号,但均已在系统中"
                }
            
            if not imported_ids:
                return {
                    "success": False,
                    "team_id": None,
                    "message": None,
                    "error": "未发现可导入的 Team 账号"
                }

            await db_session.commit()

            message = f"成功导入 {len(imported_ids)} 个 Team 账号"
            if skipped_ids:
                message += f" (另有 {len(skipped_ids)} 个已存在)"

            logger.info(f"Team 导入成功: {email}, 共 {len(imported_ids)} 个账号")

            return {
                "success": True,
                "team_id": imported_ids[0],
                "message": message,
                "error": None
            }

        except Exception as e:
            await db_session.rollback()
            logger.error(f"Team 导入失败: {e}")
            return {
                "success": False,
                "team_id": None,
                "message": None,
                "error": f"导入失败: {str(e)}"
            }


    async def update_team(
        self,
        team_id: int,
        db_session: AsyncSession,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        session_token: Optional[str] = None,
        client_id: Optional[str] = None,
        email: Optional[str] = None,
        account_id: Optional[str] = None,
        max_members: Optional[int] = None,
        status: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        更新 Team 信息

        Args:
            team_id: Team ID
            db_session: 数据库会话
            access_token: 新的 AT Token (可选)
            email: 新的邮箱 (可选)
            account_id: 新的 Account ID (可选)
            max_members: 最大成员数 (可选)

        Returns:
            结果字典
        """
        try:
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {"success": False, "error": f"Team ID {team_id} 不存在"}

            if access_token:
                team.access_token_encrypted = encryption_service.encrypt_token(access_token)
            if refresh_token:
                team.refresh_token_encrypted = encryption_service.encrypt_token(refresh_token)
            if session_token:
                team.session_token_encrypted = encryption_service.encrypt_token(session_token)
            if client_id:
                team.client_id = client_id
            if email:
                team.email = email
            if account_id:
                team.account_id = account_id
            if max_members is not None:
                team.max_members = max_members
            if status:
                team.status = status

            # 更新状态
            if team.current_members >= team.max_members:
                team.status = "full"
            elif team.status == "full" and team.current_members < team.max_members:
                team.status = "active"

            await db_session.commit()
            logger.info(f"Team {team_id} 信息更新成功")
            return {"success": True, "message": "Team 信息更新成功"}

        except Exception as e:
            await db_session.rollback()
            logger.error(f"更新 Team 失败: {e}")
            return {"success": False, "error": f"更新失败: {str(e)}"}

    async def get_team_info(self, team_id: int, db_session: AsyncSession) -> Dict[str, Any]:
        """获取 Team 详细信息 (含解密 Token)"""
        try:
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {"success": False, "error": "Team 不存在"}

            # 解密 Token
            access_token = ""
            try:
                access_token = encryption_service.decrypt_token(team.access_token_encrypted)
            except Exception as e:
                logger.error(f"解密 Token 失败: {e}")

            return {
                "success": True,
                "team": {
                    "id": team.id,
                    "email": team.email,
                    "account_id": team.account_id,
                    "max_members": team.max_members,
                    "access_token": access_token,
                    "refresh_token": encryption_service.decrypt_token(team.refresh_token_encrypted) if team.refresh_token_encrypted else "",
                    "session_token": encryption_service.decrypt_token(team.session_token_encrypted) if team.session_token_encrypted else "",
                    "client_id": team.client_id or "",
                    "team_name": team.team_name,
                    "status": team.status
                }
            }
        except Exception as e:
            logger.error(f"获取 Team 信息失败: {e}")
            return {"success": False, "error": str(e)}

    async def import_team_batch(
        self,
        text: str,
        db_session: AsyncSession
    ):
        """
        批量导入 Team (流式返回进度)

        Args:
            text: 包含 Token、邮箱、Account ID 的文本
            db_session: 数据库会话

        Yields:
            各阶段进度的 Dict
        """
        try:
            # 1. 解析文本
            parsed_data = self.token_parser.parse_team_import_text(text)

            if not parsed_data:
                yield {
                    "type": "error",
                    "error": "未能从文本中提取任何 Token"
                }
                return

            total = len(parsed_data)
            yield {
                "type": "start",
                "total": total
            }

            # 2. 逐个导入
            success_count = 0
            failed_count = 0

            for i, data in enumerate(parsed_data):
                result = await self.import_team_single(
                    access_token=data.get("token"),
                    db_session=db_session,
                    email=data.get("email"),
                    account_id=data.get("account_id"),
                    refresh_token=data.get("refresh_token"),
                    session_token=data.get("session_token"),
                    client_id=data.get("client_id")
                )

                if result["success"]:
                    success_count += 1
                else:
                    failed_count += 1

                yield {
                    "type": "progress",
                    "current": i + 1,
                    "total": total,
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "last_result": {
                        "email": data.get("email", "未知"),
                        "account_id": data.get("account_id", "未指定"),
                        "success": result["success"],
                        "team_id": result["team_id"],
                        "message": result["message"],
                        "error": result["error"]
                    }
                }

            logger.info(f"批量导入完成: 总数 {total}, 成功 {success_count}, 失败 {failed_count}")

            yield {
                "type": "finish",
                "total": total,
                "success_count": success_count,
                "failed_count": failed_count
            }

        except Exception as e:
            logger.error(f"批量导入失败: {e}")
            yield {
                "type": "error",
                "error": f"批量导入过程中发生异常: {str(e)}"
            }

    async def sync_team_info(
        self,
        team_id: int,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        同步单个 Team 的信息

        Args:
            team_id: Team ID
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, message, error
        """
        try:
            # 1. 查询 Team
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {
                    "success": False,
                    "message": None,
                    "error": f"Team ID {team_id} 不存在"
                }

            # 2. 确保 AT Token 有效
            access_token = await self.ensure_access_token(team, db_session)
            if not access_token:
                return {
                    "success": False,
                    "message": None,
                    "error": "Token 已过期且无法刷新"
                }

            # 3. 获取账户信息
            account_result = await self.chatgpt_service.get_account_info(
                access_token,
                db_session
            )

            if not account_result["success"]:
                # 检查是否封号或 Token 失效
                if await self._handle_api_error(account_result, team, db_session):
                    error_msg = account_result.get("error", "未知错误")
                    if account_result.get("error_code") == "account_deactivated":
                        error_msg = "账号已封禁 (account_deactivated)"
                    elif account_result.get("error_code") == "token_invalidated":
                        error_msg = "Token 已失效 (token_invalidated)"
                        
                    return {
                        "success": False,
                        "message": None,
                        "error": error_msg
                    }

                # 更新状态为 error
                team.status = "error"
                await db_session.commit()
                return {
                    "success": False,
                    "message": None,
                    "error": f"获取账户信息失败: {account_result['error']}"
                }

            # 4. 查找当前使用的 account
            team_accounts = account_result["accounts"]
            current_account = None

            for acc in team_accounts:
                if acc["account_id"] == team.account_id:
                    current_account = acc
                    break

            if not current_account:
                # 如果当前 account_id 不存在,使用第一个活跃的
                for acc in team_accounts:
                    if acc["has_active_subscription"]:
                        current_account = acc
                        break

                if not current_account and team_accounts:
                    current_account = team_accounts[0]

            if not current_account:
                team.status = "error"
                await db_session.commit()
                return {
                    "success": False,
                    "message": None,
                    "error": "该 Token 没有关联任何 Team 账户"
                }

            # 5. 获取成员列表
            members_result = await self.chatgpt_service.get_members(
                access_token,
                current_account["account_id"],
                db_session
            )

            current_members = 0
            if members_result["success"]:
                current_members = members_result["total"]

            # 6. 解析过期时间
            expires_at = None
            if current_account["expires_at"]:
                try:
                    expires_at = datetime.fromisoformat(
                        current_account["expires_at"].replace("+00:00", "")
                    )
                except Exception as e:
                    logger.warning(f"解析过期时间失败: {e}")

            # 7. 确定状态
            status = "active"
            if current_members >= 6:
                status = "full"
            elif expires_at and expires_at < datetime.now():
                status = "expired"

            # 8. 更新 Team 信息
            team.account_id = current_account["account_id"]
            team.team_name = current_account["name"]
            team.plan_type = current_account["plan_type"]
            team.subscription_plan = current_account["subscription_plan"]
            team.expires_at = expires_at
            team.current_members = current_members
            team.status = status
            team.last_sync = get_now()

            await db_session.commit()

            logger.info(f"Team 同步成功: ID {team_id}, 成员数 {current_members}")

            return {
                "success": True,
                "message": f"同步成功,当前成员数: {current_members}",
                "error": None
            }

        except Exception as e:
            await db_session.rollback()
            logger.error(f"Team 同步失败: {e}")
            return {
                "success": False,
                "message": None,
                "error": f"同步失败: {str(e)}"
            }

    async def sync_all_teams(
        self,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        同步所有 Team 的信息

        Args:
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, total, success_count, failed_count, results
        """
        try:
            # 1. 查询所有 Team
            stmt = select(Team)
            result = await db_session.execute(stmt)
            teams = result.scalars().all()

            if not teams:
                return {
                    "success": True,
                    "total": 0,
                    "success_count": 0,
                    "failed_count": 0,
                    "results": [],
                    "error": None
                }

            # 2. 逐个同步
            results = []
            success_count = 0
            failed_count = 0

            for team in teams:
                result = await self.sync_team_info(team.id, db_session)

                if result["success"]:
                    success_count += 1
                else:
                    failed_count += 1

                results.append({
                    "team_id": team.id,
                    "email": team.email,
                    "success": result["success"],
                    "message": result["message"],
                    "error": result["error"]
                })

            logger.info(f"批量同步完成: 总数 {len(teams)}, 成功 {success_count}, 失败 {failed_count}")

            return {
                "success": True,
                "total": len(teams),
                "success_count": success_count,
                "failed_count": failed_count,
                "results": results,
                "error": None
            }

        except Exception as e:
            logger.error(f"批量同步失败: {e}")
            return {
                "success": False,
                "total": 0,
                "success_count": 0,
                "failed_count": 0,
                "results": [],
                "error": f"批量同步失败: {str(e)}"
            }

    async def get_team_members(
        self,
        team_id: int,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        获取 Team 成员列表

        Args:
            team_id: Team ID
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, members, total, error
        """
        try:
            # 1. 查询 Team
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {
                    "success": False,
                    "members": [],
                    "total": 0,
                    "error": f"Team ID {team_id} 不存在"
                }

            # 2. 确保 AT Token 有效
            access_token = await self.ensure_access_token(team, db_session)
            if not access_token:
                return {
                    "success": False,
                    "members": [],
                    "total": 0,
                    "error": "Token 已过期且无法刷新"
                }

            # 3. 调用 ChatGPT API 获取成员列表
            members_result = await self.chatgpt_service.get_members(
                access_token,
                team.account_id,
                db_session
            )

            if not members_result["success"]:
                return {
                    "success": False,
                    "members": [],
                    "total": 0,
                    "error": f"获取成员列表失败: {members_result['error']}"
                }

            # 4. 调用 ChatGPT API 获取邀请列表
            invites_result = await self.chatgpt_service.get_invites(
                access_token,
                team.account_id,
                db_session
            )

            # 5. 合并列表并统一格式
            all_members = []
            
            # 处理已加入成员
            for m in members_result["members"]:
                all_members.append({
                    "user_id": m.get("id"),
                    "email": m.get("email"),
                    "name": m.get("name"),
                    "role": m.get("role"),
                    "added_at": m.get("created_time"),
                    "status": "joined"
                })
            
            # 处理待加入成员
            if invites_result["success"]:
                for inv in invites_result["items"]:
                    all_members.append({
                        "user_id": None, # 邀请还没有 user_id
                        "email": inv.get("email_address"),
                        "name": None,
                        "role": inv.get("role"),
                        "added_at": inv.get("created_time"),
                        "status": "invited"
                    })

            logger.info(f"获取 Team {team_id} 成员列表成功: 共 {len(all_members)} 个成员 (已加入: {members_result['total']})")

            return {
                "success": True,
                "members": all_members,
                "total": len(all_members),
                "error": None
            }

        except Exception as e:
            logger.error(f"获取成员列表失败: {e}")
            return {
                "success": False,
                "members": [],
                "total": 0,
                "error": f"获取成员列表失败: {str(e)}"
            }

    async def revoke_team_invite(
        self,
        team_id: int,
        email: str,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        撤回 Team 邀请

        Args:
            team_id: Team ID
            email: 邀请邮箱
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, message, error
        """
        try:
            # 1. 查询 Team
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {
                    "success": False,
                    "message": None,
                    "error": f"Team ID {team_id} 不存在"
                }

            # 2. 确保 AT Token 有效
            access_token = await self.ensure_access_token(team, db_session)
            if not access_token:
                return {
                    "success": False,
                    "message": None,
                    "error": "Token 已过期且无法刷新"
                }

            # 3. 调用 ChatGPT API 撤回邀请
            revoke_result = await self.chatgpt_service.delete_invite(
                access_token,
                team.account_id,
                email,
                db_session
            )

            if not revoke_result["success"]:
                # 检查是否封号或 Token 失效
                if await self._handle_api_error(revoke_result, team, db_session):
                    error_msg = revoke_result.get("error", "未知错误")
                    if revoke_result.get("error_code") == "account_deactivated":
                        error_msg = "账号已封禁 (account_deactivated)"
                    elif revoke_result.get("error_code") == "token_invalidated":
                        error_msg = "Token 已失效 (token_invalidated)"
                        
                    return {
                        "success": False,
                        "message": None,
                        "error": error_msg
                    }

                return {
                    "success": False,
                    "message": None,
                    "error": f"撤回邀请失败: {revoke_result['error']}"
                }

            # 4. 更新成员数 (如果是按席位算的，撤回邀请应该减少)
            if team.current_members > 0:
                team.current_members -= 1
            
            if team.current_members < team.max_members:
                if team.status == "full":
                    team.status = "active"

            await db_session.commit()

            logger.info(f"撤回邀请成功: {email} from Team {team_id}")

            return {
                "success": True,
                "message": f"已撤回对 {email} 的邀请",
                "error": None
            }

        except Exception as e:
            await db_session.rollback()
            logger.error(f"撤回邀请失败: {e}")
            return {
                "success": False,
                "message": None,
                "error": f"撤回邀请失败: {str(e)}"
            }

    async def add_team_member(
        self,
        team_id: int,
        email: str,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        添加 Team 成员

        Args:
            team_id: Team ID
            email: 成员邮箱
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, message, error
        """
        try:
            # 1. 查询 Team
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {
                    "success": False,
                    "message": None,
                    "error": f"Team ID {team_id} 不存在"
                }

            # 2. 检查 Team 状态
            if team.status == "full":
                return {
                    "success": False,
                    "message": None,
                    "error": "Team 已满,无法添加成员"
                }

            if team.status == "expired":
                return {
                    "success": False,
                    "message": None,
                    "error": "Team 已过期,无法添加成员"
                }

            # 3. 确保 AT Token 有效
            access_token = await self.ensure_access_token(team, db_session)
            if not access_token:
                return {
                    "success": False,
                    "message": None,
                    "error": "Token 已过期且无法刷新"
                }

            # 4. 调用 ChatGPT API 发送邀请
            invite_result = await self.chatgpt_service.send_invite(
                access_token,
                team.account_id,
                email,
                db_session
            )

            if not invite_result["success"]:
                # 检查是否封号或 Token 失效
                if await self._handle_api_error(invite_result, team, db_session):
                    error_msg = invite_result.get("error", "未知错误")
                    if invite_result.get("error_code") == "account_deactivated":
                        error_msg = "账号已封禁 (account_deactivated)"
                    elif invite_result.get("error_code") == "token_invalidated":
                        error_msg = "Token 已失效 (token_invalidated)"
                        
                    return {
                        "success": False,
                        "message": None,
                        "error": error_msg
                    }

                return {
                    "success": False,
                    "message": None,
                    "error": f"发送邀请失败: {invite_result['error']}"
                }

            # 5. 更新成员数
            team.current_members += 1
            if team.current_members >= team.max_members:
                team.status = "full"

            await db_session.commit()

            logger.info(f"添加成员成功: {email} -> Team {team_id}")

            return {
                "success": True,
                "message": f"邀请已发送到 {email}",
                "error": None
            }

        except Exception as e:
            await db_session.rollback()
            logger.error(f"添加成员失败: {e}")
            return {
                "success": False,
                "message": None,
                "error": f"添加成员失败: {str(e)}"
            }

    async def delete_team_member(
        self,
        team_id: int,
        user_id: str,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        删除 Team 成员

        Args:
            team_id: Team ID
            user_id: 用户 ID (格式: user-xxx)
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, message, error
        """
        try:
            # 1. 查询 Team
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {
                    "success": False,
                    "message": None,
                    "error": f"Team ID {team_id} 不存在"
                }

            # 2. 确保 AT Token 有效
            access_token = await self.ensure_access_token(team, db_session)
            if not access_token:
                return {
                    "success": False,
                    "message": None,
                    "error": "Token 已过期且无法刷新"
                }

            # 3. 调用 ChatGPT API 删除成员
            delete_result = await self.chatgpt_service.delete_member(
                access_token,
                team.account_id,
                user_id,
                db_session
            )

            if not delete_result["success"]:
                # 检查是否封号或 Token 失效
                if await self._handle_api_error(delete_result, team, db_session):
                    error_msg = delete_result.get("error", "未知错误")
                    if delete_result.get("error_code") == "account_deactivated":
                        error_msg = "账号已封禁 (account_deactivated)"
                    elif delete_result.get("error_code") == "token_invalidated":
                        error_msg = "Token 已失效 (token_invalidated)"
                        
                    return {
                        "success": False,
                        "message": None,
                        "error": error_msg
                    }

                return {
                    "success": False,
                    "message": None,
                    "error": f"删除成员失败: {delete_result['error']}"
                }

            # 4. 更新成员数
            if team.current_members > 0:
                team.current_members -= 1

            # 更新状态
            if team.current_members < team.max_members:
                if team.status == "full":
                    team.status = "active"

            await db_session.commit()

            logger.info(f"删除成员成功: {user_id} from Team {team_id}")

            return {
                "success": True,
                "message": "成员已删除",
                "error": None
            }

        except Exception as e:
            await db_session.rollback()
            logger.error(f"删除成员失败: {e}")
            return {
                "success": False,
                "message": None,
                "error": f"删除成员失败: {str(e)}"
            }

    async def get_available_teams(
        self,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        获取可用的 Team 列表 (用于用户兑换页面)

        Args:
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, teams, error
        """
        try:
            # 查询 status='active' 且 current_members < max_members 的 Team
            stmt = select(Team).where(
                Team.status == "active",
                Team.current_members < Team.max_members
            )
            result = await db_session.execute(stmt)
            teams = result.scalars().all()

            # 构建返回数据 (不包含敏感信息)
            team_list = []
            for team in teams:
                team_list.append({
                    "id": team.id,
                    "team_name": team.team_name,
                    "current_members": team.current_members,
                    "max_members": team.max_members,
                    "expires_at": team.expires_at.isoformat() if team.expires_at else None,
                    "subscription_plan": team.subscription_plan
                })

            logger.info(f"获取可用 Team 列表成功: 共 {len(team_list)} 个")

            return {
                "success": True,
                "teams": team_list,
                "error": None
            }

        except Exception as e:
            logger.error(f"获取可用 Team 列表失败: {e}")
            return {
                "success": False,
                "teams": [],
                "error": f"获取列表失败: {str(e)}"
            }

    async def get_total_available_spots(
        self,
        db_session: AsyncSession
    ) -> int:
        """
        获取剩余车位总数

        Args:
            db_session: 数据库会话

        Returns:
            剩余车位总数
        """
        try:
            # 计算所有 active Team 的剩余车位总和
            # remaining = max_members - current_members
            stmt = select(
                func.sum(Team.max_members - Team.current_members)
            ).where(
                Team.status == "active",
                Team.current_members < Team.max_members
            )
            
            result = await db_session.execute(stmt)
            total_spots = result.scalar() or 0
            
            return int(total_spots)

        except Exception as e:
            logger.error(f"获取剩余车位失败: {e}")
            return 0



    async def get_team_by_id(
        self,
        team_id: int,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        根据 ID 获取 Team 详情

        Args:
            team_id: Team ID
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, team, team_accounts, error
        """
        try:
            # 查询 Team (包含关联的 team_accounts)
            stmt = select(Team).where(Team.id == team_id).options(
                selectinload(Team.team_accounts)
            )
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {
                    "success": False,
                    "team": None,
                    "team_accounts": [],
                    "error": f"Team ID {team_id} 不存在"
                }

            # 解密 Access Token
            try:
                from app.services.encryption import encryption_service
                access_token = encryption_service.decrypt_token(team.access_token_encrypted)
            except Exception as e:
                logger.error(f"解密 Team {team_id} Token 失败: {e}")
                access_token = None

            # 构建返回数据
            team_data = {
                "id": team.id,
                "email": team.email,
                "account_id": team.account_id,
                "access_token": access_token,
                "team_name": team.team_name,
                "plan_type": team.plan_type,
                "subscription_plan": team.subscription_plan,
                "expires_at": team.expires_at.isoformat() if team.expires_at else None,
                "current_members": team.current_members,
                "max_members": team.max_members,
                "status": team.status,
                "last_sync": team.last_sync.isoformat() if team.last_sync else None,
                "created_at": team.created_at.isoformat() if team.created_at else None
            }

            team_accounts_data = []
            for acc in team.team_accounts:
                team_accounts_data.append({
                    "id": acc.id,
                    "account_id": acc.account_id,
                    "account_name": acc.account_name,
                    "is_primary": acc.is_primary
                })

            logger.info(f"获取 Team {team_id} 详情成功")

            return {
                "success": True,
                "team": team_data,
                "team_accounts": team_accounts_data,
                "error": None
            }

        except Exception as e:
            logger.error(f"获取 Team 详情失败: {e}")
            return {
                "success": False,
                "team": None,
                "team_accounts": [],
                "error": f"获取 Team 详情失败: {str(e)}"
            }

    async def get_all_teams(
        self,
        db_session: AsyncSession,
        page: int = 1,
        per_page: int = 20,
        search: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取所有 Team 列表 (用于管理员页面)

        Args:
            db_session: 数据库会话
            page: 页码
            per_page: 每页数量
            search: 搜索关键词

        Returns:
            结果字典,包含 success, teams, total, total_pages, current_page, error
        """
        try:
            # 1. 构建查询语句
            stmt = select(Team)
            
            # 2. 如果有搜索词,添加过滤条件
            if search:
                from sqlalchemy import or_, cast, String
                search_filter = f"%{search}%"
                stmt = stmt.where(
                    or_(
                        Team.email.ilike(search_filter),
                        Team.account_id.ilike(search_filter),
                        Team.team_name.ilike(search_filter),
                        cast(Team.id, String).ilike(search_filter)
                    )
                )

            # 3. 获取总数
            count_stmt = select(func.count()).select_from(stmt.subquery())
            count_result = await db_session.execute(count_stmt)
            total = count_result.scalar() or 0

            # 4. 计算分页
            import math
            total_pages = math.ceil(total / per_page) if total > 0 else 1
            if page < 1:
                page = 1
            if total_pages > 0 and page > total_pages:
                page = total_pages
            
            offset = (page - 1) * per_page

            # 5. 查询分页数据
            final_stmt = stmt.order_by(Team.created_at.desc()).limit(per_page).offset(offset)
            result = await db_session.execute(final_stmt)
            teams = result.scalars().all()

            # 构建返回数据
            team_list = []
            for team in teams:
                team_list.append({
                    "id": team.id,
                    "email": team.email,
                    "account_id": team.account_id,
                    "team_name": team.team_name,
                    "plan_type": team.plan_type,
                    "subscription_plan": team.subscription_plan,
                    "expires_at": team.expires_at.isoformat() if team.expires_at else None,
                    "current_members": team.current_members,
                    "max_members": team.max_members,
                    "status": team.status,
                    "last_sync": team.last_sync.isoformat() if team.last_sync else None,
                    "created_at": team.created_at.isoformat() if team.created_at else None
                })

            logger.info(f"获取所有 Team 列表成功: 第 {page} 页, 共 {len(team_list)} 个 / 总数 {total}")

            return {
                "success": True,
                "teams": team_list,
                "total": total,
                "total_pages": total_pages,
                "current_page": page,
                "error": None
            }

        except Exception as e:
            logger.error(f"获取所有 Team 列表失败: {e}")
            return {
                "success": False,
                "teams": [],
                "error": f"获取所有 Team 列表失败: {str(e)}"
            }

    async def update_team(
        self,
        team_id: int,
        db_session: AsyncSession,
        email: Optional[str] = None,
        account_id: Optional[str] = None,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        session_token: Optional[str] = None,
        client_id: Optional[str] = None,
        max_members: Optional[int] = None,
        status: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        更新 Team 信息

        Args:
            team_id: Team ID
            db_session: 数据库会话
            email: 新邮箱 (可选)
            account_id: 新 account_id (可选,用于切换多 Team)

        Returns:
            结果字典,包含 success, message, error
        """
        try:
            # 1. 查询 Team (包含关联的 team_accounts)
            stmt = select(Team).where(Team.id == team_id).options(
                selectinload(Team.team_accounts)
            )
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {
                    "success": False,
                    "message": None,
                    "error": f"Team ID {team_id} 不存在"
                }

            # 2. 更新属性
            if email:
                team.email = email

            if account_id:
                team.account_id = account_id
                # 更新关联账户的主次状态
                for acc in team.team_accounts:
                    if acc.account_id == account_id:
                        acc.is_primary = True
                    else:
                        acc.is_primary = False

            # 4. 更新 Access Token
            if access_token:
                try:
                    from app.services.encryption import encryption_service
                    team.access_token_encrypted = encryption_service.encrypt_token(access_token)
                except Exception as e:
                    logger.error(f"重新加密 Team {team_id} Token 失败: {e}")
                    return {
                        "success": False,
                        "message": None,
                        "error": f"加密 Token 失败: {str(e)}"
                    }

            # 5. 更新最大成员数
            if max_members is not None:
                team.max_members = max_members
                # 更新状态
                if team.current_members >= max_members:
                    if team.status == "active":
                        team.status = "full"
                elif team.status == "full":
                    team.status = "active"

            # 6. 更新状态 (手动覆盖)
            if status:
                team.status = status

            await db_session.commit()

            # 6. 如果更新了 AT 或 account_id，触发一次同步以确保状态正确
            if access_token or account_id:
                await self.sync_team_info(team_id, db_session)

            logger.info(f"更新 Team {team_id} 成功")

            return {
                "success": True,
                "message": "Team 信息已更新",
                "error": None
            }

        except Exception as e:
            await db_session.rollback()
            logger.error(f"更新 Team 失败: {e}")
            return {
                "success": False,
                "message": None,
                "error": f"更新 Team 失败: {str(e)}"
            }

    async def delete_team(
        self,
        team_id: int,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        删除 Team

        Args:
            team_id: Team ID
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, message, error
        """
        try:
            # 1. 查询 Team
            stmt = select(Team).where(Team.id == team_id)
            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {
                    "success": False,
                    "message": None,
                    "error": f"Team ID {team_id} 不存在"
                }

            # 2. 删除 Team (级联删除 team_accounts 和 redemption_records)
            await db_session.delete(team)
            await db_session.commit()

            logger.info(f"删除 Team {team_id} 成功")

            return {
                "success": True,
                "message": "Team 已删除",
                "error": None
            }

        except Exception as e:
            await db_session.rollback()
            logger.error(f"删除 Team 失败: {e}")
            return {
                "success": False,
                "message": None,
                "error": f"删除 Team 失败: {str(e)}"
            }


# 创建全局 Team 服务实例
team_service = TeamService()
