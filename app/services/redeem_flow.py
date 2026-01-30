"""
兑换流程服务
协调用户兑换流程，包括验证、Team选择、邀请发送、事务处理和并发控制
"""
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Team, RedemptionCode, RedemptionRecord
from app.services.redemption import RedemptionService
from app.services.warranty import WarrantyService
from app.services.team import TeamService
from app.services.chatgpt import ChatGPTService
from app.services.encryption import encryption_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


class RedeemFlowService:
    """兑换流程服务类"""

    def __init__(self):
        """初始化兑换流程服务"""
        from app.services.chatgpt import chatgpt_service
        self.redemption_service = RedemptionService()
        self.warranty_service = WarrantyService()
        self.team_service = TeamService()
        self.chatgpt_service = chatgpt_service

    async def verify_code_and_get_teams(
        self,
        code: str,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        验证兑换码并获取可用 Team 列表

        Args:
            code: 兑换码
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, valid, reason, teams, error
        """
        try:
            # 1. 验证兑换码
            validate_result = await self.redemption_service.validate_code(code, db_session)

            if not validate_result["success"]:
                return {
                    "success": False,
                    "valid": False,
                    "reason": None,
                    "teams": [],
                    "error": validate_result["error"]
                }

            if not validate_result["valid"]:
                return {
                    "success": True,
                    "valid": False,
                    "reason": validate_result["reason"],
                    "teams": [],
                    "error": None
                }

            # 2. 获取可用 Team 列表
            teams_result = await self.team_service.get_available_teams(db_session)

            if not teams_result["success"]:
                return {
                    "success": False,
                    "valid": True,
                    "reason": None,
                    "teams": [],
                    "error": teams_result["error"]
                }

            logger.info(f"验证兑换码成功: {code}, 可用 Team 数量: {len(teams_result['teams'])}")

            return {
                "success": True,
                "valid": True,
                "reason": None,
                "teams": teams_result["teams"],
                "error": None
            }

        except Exception as e:
            logger.error(f"验证兑换码并获取 Team 列表失败: {e}")
            return {
                "success": False,
                "valid": False,
                "reason": None,
                "teams": [],
                "error": f"验证失败: {str(e)}"
            }

    async def select_team_auto(
        self,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        自动选择 Team (选择过期时间最早的)

        Args:
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, team_id, error
        """
        try:
            # 查询可用 Team，按过期时间升序排序
            stmt = select(Team).where(
                Team.status == "active",
                Team.current_members < Team.max_members
            ).order_by(Team.expires_at.asc()).limit(1)

            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                return {
                    "success": False,
                    "team_id": None,
                    "error": "没有可用的 Team"
                }

            logger.info(f"自动选择 Team: {team.id} (过期时间: {team.expires_at})")

            return {
                "success": True,
                "team_id": team.id,
                "error": None
            }

        except Exception as e:
            logger.error(f"自动选择 Team 失败: {e}")
            return {
                "success": False,
                "team_id": None,
                "error": f"自动选择 Team 失败: {str(e)}"
            }

    async def redeem_and_join_team(
        self,
        email: str,
        code: str,
        team_id: Optional[int],
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        完整的兑换流程 (带事务和并发控制)
        优化版本: 将网络请求移出写事务,避免 SQLite 锁定
        """
        team_id_final = None
        try:
            # --- 阶段 1: 验证并占位 (短事务) ---
            # 显式管理事务
            async with db_session.begin():
                # 1. 验证兑换码
                validate_result = await self.redemption_service.validate_code(code, db_session)

                if not validate_result["success"]:
                    return {"success": False, "error": validate_result["error"]}
                if not validate_result["valid"]:
                    return {"success": False, "error": validate_result["reason"]}

                # 2. 选择 Team
                if team_id is None:
                    select_result = await self.select_team_auto(db_session)
                    if not select_result["success"]:
                        return {"success": False, "error": select_result["error"]}
                    team_id_final = select_result["team_id"]
                else:
                    team_id_final = team_id

                # 3. 锁定并检查 Team
                stmt = select(Team).where(Team.id == team_id_final).with_for_update()
                result = await db_session.execute(stmt)
                team = result.scalar_one_or_none()

                if not team:
                    return {"success": False, "error": f"Team ID {team_id_final} 不存在"}
                
                if team.current_members >= team.max_members:
                    return {"success": False, "error": "Team 已满，请选择其他 Team"}
                
                if team.status != "active":
                    return {"success": False, "error": f"Team 状态异常: {team.status}"}

                # 4. 锁定并更新兑换码状态 (先占位，防止并发使用同一码)
                stmt = select(RedemptionCode).where(RedemptionCode.code == code).with_for_update()
                result = await db_session.execute(stmt)
                redemption_code = result.scalar_one_or_none()
                
                if not redemption_code:
                    return {"success": False, "error": "兑换码不存在"}
                
                # 特殊处理质保码
                is_warranty_code = redemption_code.has_warranty
                is_first_use = redemption_code.status == "unused"
                
                if not is_first_use:
                    # 如果不是首次使用，检查是否为质保码且可重复使用
                    if is_warranty_code:
                        # 验证质保码是否可重复使用
                        warranty_check = await self.warranty_service.validate_warranty_reuse(
                            db_session, code, email
                        )
                        if not warranty_check["success"] or not warranty_check["can_reuse"]:
                            return {"success": False, "error": warranty_check.get("reason", "兑换码已被使用")}
                    else:
                        return {"success": False, "error": "兑换码已被占用或失效"}

                # 更新兑换码状态
                if is_warranty_code:
                    # 质保码使用特殊状态
                    redemption_code.status = "warranty_active"
                    # 首次使用时设置质保到期时间
                    if is_first_use:
                        warranty_days = redemption_code.warranty_days or 30
                        redemption_code.warranty_expires_at = get_now() + timedelta(days=warranty_days)
                else:
                    # 普通码标记为已使用
                    redemption_code.status = "used"
                
                redemption_code.used_by_email = email
                redemption_code.used_team_id = team_id_final
                redemption_code.used_at = get_now()

                # 增加 Team 成员数占位
                team.current_members += 1
                if team.current_members >= team.max_members:
                    team.status = "full"
                
                # 记录 Team 信息以便阶段 2 使用
                final_team_account_id = team.account_id
                final_team_name = team.team_name
                final_team_expires_at = team.expires_at
                final_access_token_encrypted = team.access_token_encrypted
                final_is_warranty = is_warranty_code
                
                # 事务会自动 commit
            
            # --- 阶段 2: 网络请求 (非阻塞事务) ---
            # 5. 解密 AT Token
            try:
                access_token = encryption_service.decrypt_token(final_access_token_encrypted)
            except Exception as e:
                logger.error(f"解密 Token 失败: {e}")
                # 需要回退
                await self._rollback_redemption(db_session, code, team_id_final)
                return {"success": False, "error": f"解密 Token 失败: {str(e)}"}

            # 6. 调用 ChatGPT API 发送邀请
            invite_result = await self.chatgpt_service.send_invite(
                access_token,
                final_team_account_id,
                email,
                db_session
            )

            # --- 阶段 3: 最终化或回滚 ---
            if invite_result["success"]:
                # 7. 成功：补全记录
                async with db_session.begin():
                    redemption_record = RedemptionRecord(
                        email=email,
                        code=code,
                        team_id=team_id_final,
                        account_id=final_team_account_id,
                        is_warranty_redemption=final_is_warranty
                    )
                    db_session.add(redemption_record)
                
                logger.info(f"兑换成功: {email} 加入 Team {team_id_final} (兑换码: {code})")
                return {
                    "success": True,
                    "message": f"成功加入 Team: {final_team_name}",
                    "team_info": {
                        "team_id": team_id_final,
                        "team_name": final_team_name,
                        "account_id": final_team_account_id,
                        "expires_at": final_team_expires_at.isoformat() if final_team_expires_at else None
                    },
                    "error": None
                }
            else:
                # 8. 失败：回退阶段 1 的占位
                logger.warning(f"API 发送邀请失败，执行回退: {invite_result['error']}")
                await self._rollback_redemption(db_session, code, team_id_final)
                return {
                    "success": False,
                    "error": f"发送邀请失败: {invite_result['error']}"
                }

        except Exception as e:
            logger.error(f"兑换流程异常: {e}")
            # 如果在阶段 2 或 3 发生未捕获异常，尝试回退
            if team_id_final:
                try:
                    await self._rollback_redemption(db_session, code, team_id_final)
                except:
                    pass
            return {
                "success": False,
                "error": f"兑换系统异常: {str(e)}"
            }

    async def _rollback_redemption(
        self,
        db_session: AsyncSession,
        code: str,
        team_id: int
    ):
        """回退兑换占位"""
        try:
            async with db_session.begin():
                # 回退兑换码状态
                stmt = select(RedemptionCode).where(RedemptionCode.code == code).with_for_update()
                result = await db_session.execute(stmt)
                redemption_code = result.scalar_one_or_none()
                if redemption_code:
                    # 质保码回退到 warranty_active 或 unused
                    if redemption_code.has_warranty:
                        # 检查是否有其他成功的兑换记录
                        stmt = select(RedemptionRecord).where(
                            RedemptionRecord.code == code
                        ).order_by(RedemptionRecord.redeemed_at.desc())
                        result = await db_session.execute(stmt)
                        other_record = result.scalars().first()
                        
                        if other_record:
                            # 有其他记录，恢复为最后一次成功的状态
                            redemption_code.status = "warranty_active"
                            redemption_code.used_by_email = other_record.email
                            redemption_code.used_team_id = other_record.team_id
                            redemption_code.used_at = other_record.redeemed_at
                        else:
                            # 没有其他成功记录，彻底回退到未使用
                            redemption_code.status = "unused"
                            redemption_code.warranty_expires_at = None
                            redemption_code.used_by_email = None
                            redemption_code.used_team_id = None
                            redemption_code.used_at = None
                    else:
                        # 普通码彻底回退到 unused
                        redemption_code.status = "unused"
                        redemption_code.used_by_email = None
                        redemption_code.used_team_id = None
                        redemption_code.used_at = None

                # 回退 Team 计数
                stmt = select(Team).where(Team.id == team_id).with_for_update()
                result = await db_session.execute(stmt)
                team = result.scalar_one_or_none()
                if team:
                    if team.current_members > 0:
                        team.current_members -= 1
                    if team.status == "full" and team.current_members < team.max_members:
                        team.status = "active"
            logger.info(f"已回退兑换占位: code={code}, team_id={team_id}")
        except Exception as e:
            logger.error(f"回退兑换占位失败: {e}")


# 创建全局实例
redeem_flow_service = RedeemFlowService()
