-- GPU 설정의 AWS 칸(S15P31A101-703, docs/design/S15P31A101-703-aws-gpu-provider.md). 추가만 한다(AGENTS 6): 설정은 저장마다 새 버전 행이고
-- 지난 버전 행에는 빈 값이 들어간다. 비밀 값(자격 증명)은 두지 않는다(EC2 .env 또는 인스턴스 역할).
-- AWS 공급자에서는 preferred_gpu_types 가 EC2 인스턴스 종류(예: g6e.2xlarge), preferred_regions 가 가용 영역(예: ap-northeast-2a),
-- interruptible 이 스팟(일회성, 중단되면 지움)이다.
ALTER TABLE gpu_settings_version
    -- EC2 시작 템플릿 ID(lt-…: AMI·디스크·키 페어·IAM 인스턴스 프로파일·보안 그룹 기본값). NULL 이면 AWS 로 켜지 않는다(503)
    ADD COLUMN aws_launch_template_id text,
    -- 가용 영역마다 하나씩 쓸 서브넷 ID(subnet-…). 빈 배열이면 가용 영역의 기본 서브넷(기본 VPC)
    ADD COLUMN aws_subnet_ids jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(aws_subnet_ids) = 'array'),
    -- 보안 그룹 ID(sg-…). 빈 배열이면 시작 템플릿의 보안 그룹
    ADD COLUMN aws_security_group_ids jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(aws_security_group_ids) = 'array');
